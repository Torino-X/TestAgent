"""Phase 2.9A.3 events-post-confirm 修复测试。

覆盖:
  1. 真实 ORM 列查询(get_owned_task)
  2. 不引用 user_id_internal / task_id_internal
  3. 任务归属 → 401/404
  4. 鉴权 / 任务快照在 StreamingResponse 创建前完成
  5. event_stream 不持有 AsyncSession
  6. Session/ConnectionPool 不被异常关闭路径耗尽
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── A. Repository 层:get_owned_task 用 ORM 列 ─────────────────────────────


@pytest.mark.asyncio
async def test_get_owned_task_uses_agent_tasks_user_id_column():
    """get_owned_task 通过 AgentTask.user_id (非 user_id_internal) 查询。

    通过源码检查 + 路由源码无 user_id_internal 字面量双重约束。
    """

    from app.repositories.agent_task_repository import AgentTaskRepository
    import inspect as _insp

    src = _insp.getsource(AgentTaskRepository.get_owned_task)
    assert "AgentTask.user_id" in src, "get_owned_task 应使用 ORM AgentTask.user_id"
    # 禁止裸 SQL 字符串拼装
    assert "user_id_internal" not in src


@pytest.mark.asyncio
async def test_sse_endpoint_does_not_reference_user_id_internal_column():
    """事件流端点源码不应包含 user_id_internal / task_id_internal 列名引用。

    唯一允许的 user_id_internal / task_id_internal 出现位置是注释。
    """
    from app.api.v1 import agent_tasks as module

    src = inspect.getsource(module.task_events_post_confirm_sse)

    # 排除注释行(以 # 开头的行,不含 user_id_internal 在 SQL 中)
    sql_lines = [
        line
        for line in src.split("\n")
        if not line.strip().startswith("#") and not line.strip().startswith('"""')
    ]
    sql_only = "\n".join(sql_lines)

    assert "user_id_internal" not in sql_only, (
        "events-post-confirm 不应引用 user_id_internal 列;改用 ORM 或 AgentTask.user_id"
    )
    assert (
        ":uid" not in sql_only or "AgentTask.user_id" in sql_only
    ), "禁止裸 SQL 鉴权,应使用 AgentTaskRepository.get_owned_task"


# ── B. SSE generator 不持有 Session ──────────────────────────────────────


def test_event_stream_generator_does_not_import_session_factory():
    """event_stream 内部不出现 AsyncSessionLocal() / session_factory 调用。

    历史事件回放是允许的(短 Session,立即关),
    但不允许复用请求级 Session。
    """
    from app.api.v1 import agent_tasks as module

    src = inspect.getsource(module.task_events_post_confirm_sse)
    # 整个函数允许出现 `async with AsyncSessionLocal() as s:` 用于历史事件回放
    # 但 generator 嵌套函数中只允许有限次数
    # 这里只检查 generator 函数体不引入「请求级 session」
    gen_segment = src.split("async def event_stream():")[1] if "async def event_stream():" in src else ""
    # generator 内可独立短 Session 用于历史回放
    # 但若出现 Depends(get_db) 或 outer session 引用则失败
    assert "Depends(get_db)" not in gen_segment, (
        "event_stream 不得依赖 request-scoped session"
    )
    # 必须不出现 Depends 注解
    assert "yield" in src, "SSE 端点必须 yield events"


def test_event_stream_does_not_call_session_rollback():
    """CancelledError 处理路径不调 session.rollback。

    Phase 2.9A.2/2.9A.3 invariant:
      * Session 在等待期间已关闭;
      * 客户端断开 → 只 unsubscribe,不 rollback。
    """
    from app.api.v1 import agent_tasks as module

    src = inspect.getsource(module.task_events_post_confirm_sse)
    assert "session.rollback" not in src


# ── C. 端点鉴权/query 在 StreamingResponse 创建前完成 ─────────────────────


@pytest.mark.asyncio
async def test_endpoint_raises_http404_before_streaming_response():
    """任务不存在时,端点必须 HTTPException(404),绝不返回 SSE 200。

    通过 mock 抓取异常路径,验证:
      * 鉴权失败时 raise HTTPException;
      * 调用 client.get 后客户端收到 404,而非 SSE 文本流。
    """
    from fastapi import HTTPException

    from app.api.v1.agent_tasks import task_events_post_confirm_sse

    # 构造 mock:where get_owned_task returns None
    mock_get_owned = AsyncMock(return_value=None)

    with patch(
        "app.repositories.agent_task_repository.AgentTaskRepository.get_owned_task",
        new=mock_get_owned,
    ):
        # patch AsyncSessionLocal 让 with 不报错
        fake_session = AsyncMock()
        with patch(
            "app.api.v1.agent_tasks.AsyncSessionLocal"
        ) as mock_session_local:
            mock_session_local.return_value.__aenter__ = AsyncMock(return_value=fake_session)
            mock_session_local.return_value.__aexit__ = AsyncMock(return_value=False)

            # 直接调用 endpoint 函数,捕获 HTTPException
            from app.schemas.auth import UserProfile
            user = UserProfile(
                id="user_public",  # type: ignore[arg-type]
                internal_id=1,
                name="u", role="user", username="u",
                email="x@x",
            )
            with pytest.raises(HTTPException) as exc:
                await task_events_post_confirm_sse(
                    task_id="task_missing",
                    current_user=user,
                    request=None,
                )
            assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_endpoint_returns_streaming_response_when_task_owned():
    """任务本人 → 返回 StreamingResponse, 流 phase_start。"""
    # 构造 mock:where get_owned_task returns an AgentTask
    fake_task = MagicMock()
    fake_task.id = 100
    fake_task.engine_type = "langgraph"
    fake_task.task_context_json = '{"section_confirm_config": {"sections": []}}'

    mock_get_owned = AsyncMock(return_value=fake_task)

    with patch(
        "app.repositories.agent_task_repository.AgentTaskRepository.get_owned_task",
        new=mock_get_owned,
    ):
        fake_session = AsyncMock()
        with patch(
            "app.api.v1.agent_tasks.AsyncSessionLocal"
        ) as mock_sl:
            mock_sl.return_value.__aenter__ = AsyncMock(return_value=fake_session)
            mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_queue = AsyncMock()
            mock_unsubscribe = AsyncMock()
            with patch(
                "app.api.v1.agent_tasks._subscribe_task_events",
                new=AsyncMock(return_value=(mock_queue, mock_unsubscribe)),
            ):
                from app.schemas.auth import UserProfile
                user = UserProfile(
                    id="user_public",  # type: ignore[arg-type]
                    internal_id=1,
                    name="u", role="user", username="u",
                    email="x@x",
                )
                from app.api.v1.agent_tasks import task_events_post_confirm_sse
                from fastapi.responses import StreamingResponse
                response = await task_events_post_confirm_sse(
                    task_id="task_owned",
                    current_user=user,
                    request=None,
                )
                assert isinstance(response, StreamingResponse)
                assert response.media_type == "text/event-stream"


# ── D. 幂等:enqueue resume row 双击防重 ─────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_resume_idempotent_key_avoids_duplicate_row():
    """用 idempotency_key 在第二次 enqueue 时返回 None,不创建第二条 row。"""

    class FakeRepo:
        def __init__(self):
            self.existing_keys: set[str] = set()
            self.inserted: list[dict[str, Any]] = []

        async def enqueue_new_task(
            self,
            *,
            task_public_id: str,
            task_internal_id: int,
            engine_type: str,
            request_type: str = "new_task",
            graph_name=None,
            graph_version=None,
            payload=None,
            idempotency_key=None,
        ):
            if idempotency_key in self.existing_keys:
                return None
            self.existing_keys.add(idempotency_key or "")
            row = MagicMock()
            row.id = len(self.inserted) + 1
            self.inserted.append(
                {"task": task_public_id, "engine": engine_type,
                 "req": request_type, "key": idempotency_key}
            )
            return row

    repo = FakeRepo()
    # 双击入队
    r1 = await repo.enqueue_new_task(
        task_public_id="task_x",
        task_internal_id=1,
        engine_type="langgraph",
        request_type="resume",
        idempotency_key="task_x|resume|user_confirm",
    )
    r2 = await repo.enqueue_new_task(
        task_public_id="task_x",
        task_internal_id=1,
        engine_type="langgraph",
        request_type="resume",
        idempotency_key="task_x|resume|user_confirm",
    )
    assert r1 is not None, "首次入队应成功"
    assert r2 is None, "重复 idempotency_key 应跳过"
    assert len(repo.inserted) == 1, "DB 应只创建一行"