"""AgentTaskService 单测 — 重点覆盖 cancel 端 NOWAIT 锁探测 (F027)。

2026-07-14：cancel 之前因为 orchestrator 长事务持 agent_tasks 行锁，
前端 cancel 静默 hang 50s 后抛 1205。修复后用 ``SELECT ... FOR UPDATE
NOWAIT`` 探锁 → 立即抛 409，3 个测试验证这条路径：

- ``test_cancel_nowait_lock_returns_409``：mock execute 抛 OperationalError(3572)
- ``test_cancel_nowait_lock_string_match_returns_409``：兜底走 str 匹配
- ``test_cancel_normal_path_no_nowait_conflict``：正常路径不受影响

2026-07-14（追加）：SSE 端点 ``events-post-confirm`` 在生产上抛
``MissingGreenlet: greenlet_spawn has not been called``，因为 caller
的 ORM ``task`` 在 ``load_checkpoint → save_checkpoint → commit()``
链路里被 expire，再次访问 ``task.id`` / ``task.task_context_json`` 时
触发 lazy load 撞 greenlet。修复方法：SSE 入口改用 raw SQL 拿
``id, conversation_id, [task_context_json]``（post-confirm 多带 ctx_json
字段），caller 全程用普通变量，不再触碰 ORM。
"""


from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError


def _make_task_row(
    public_id: str = "task_test_001",
    user_id: int = 42,
    status: str = "running",
    task_id: int = 1,
):
    """构造 AgentTask 替身，避免真的走 ORM。"""
    task = MagicMock()
    task.id = task_id
    task.public_id = public_id
    task.user_id = user_id
    task.conversation_id = 100
    task.status = status
    return task


def _make_execute_result(*, row=None):
    """构造 execute 结果对象。"""
    r = MagicMock()
    r.fetchone.return_value = row
    return r


class TestCancelNowaitLock:
    """cancel 端 SELECT ... FOR UPDATE NOWAIT 锁探测。"""

    @pytest.fixture
    def svc(self):
        """构造 AgentTaskService，所有 repo 都是 mock。"""
        from app.services.agent_task_service import AgentTaskService

        mock_session = AsyncMock()
        mock_task_repo = AsyncMock()
        mock_event_repo = AsyncMock()
        mock_confirm_repo = AsyncMock()

        with patch(
            "app.services.agent_task_service.AgentTaskRepository",
            return_value=mock_task_repo,
        ), patch(
            "app.services.agent_task_service.EventRepository",
            return_value=mock_event_repo,
        ), patch(
            "app.services.agent_task_service.ConfirmationRepository",
            return_value=mock_confirm_repo,
        ):
            service = AgentTaskService(mock_session)
            service._task_repo = mock_task_repo
            service._event_repo = mock_event_repo
            service._confirm_repo = mock_confirm_repo
            yield service, mock_session, mock_task_repo, mock_event_repo

    @pytest.mark.asyncio
    async def test_cancel_nowait_lock_returns_409(self, svc):
        """orchestrator 持锁 → SELECT FOR UPDATE NOWAIT 抛 OperationalError(3572)
        → 立即抛 AppError(40901)，不 hang 50s。"""
        from app.core.exceptions import AppError

        service, mock_session, mock_task_repo, mock_event_repo = svc

        # 构造一个 OperationalError，其 .orig.args[0] == 3572
        orig = MagicMock()
        orig.args = (3572, "Statement aborted because lock(s) could not be acquired immediately")
        op_err = OperationalError("SELECT FOR UPDATE NOWAIT failed", {}, orig)

        # 第一次 execute 抛 3572（NOWAIT 锁失败）
        mock_session.execute = AsyncMock(side_effect=[op_err])
        mock_session.commit = AsyncMock()

        with pytest.raises(AppError) as exc_info:
            await service.cancel("task_test_001", user_internal_id=42)
        # 业务码 40901（HTTP 409 走 exception_handler）
        assert exc_info.value.code == 40901
        # 提示文案要告诉用户稍后重试
        assert "稍后" in exc_info.value.message or "重试" in exc_info.value.message

        # 不能继续 commit 或 update_status（事务提前失败）
        mock_task_repo.update_status.assert_not_called()
        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_nowait_lock_string_match_returns_409(self, svc):
        """即使 err_code 不在 orig.args（异常类型奇怪），只要字符串含 NOWAIT
        也兜底识别为 409。"""
        from app.core.exceptions import AppError

        service, mock_session, mock_task_repo, mock_event_repo = svc

        # 构造一个非标准 OperationalError，没有 orig 但 message 含 NOWAIT
        class FakeOrig:
            args = (None,)

        op_err = OperationalError(
            "SELECT ... FOR UPDATE NOWAIT could not acquire lock",
            {},
            FakeOrig(),
        )

        mock_session.execute = AsyncMock(side_effect=[op_err])

        with pytest.raises(AppError) as exc_info:
            await service.cancel("task_test_001", user_internal_id=42)
        assert exc_info.value.code == 40901

    @pytest.mark.asyncio
    async def test_cancel_nowait_other_error_propagates(self, svc):
        """非 3572 / 非 NOWAIT 的 OperationalError（如 1213 deadlock）应原样抛，
        不被吞掉转成 409。"""
        service, mock_session, mock_task_repo, mock_event_repo = svc

        orig = MagicMock()
        orig.args = (1213, "Deadlock found when trying to get lock")
        op_err = OperationalError("deadlock", {}, orig)

        mock_session.execute = AsyncMock(side_effect=[op_err])

        with pytest.raises(OperationalError):
            await service.cancel("task_test_001", user_internal_id=42)

    @pytest.mark.asyncio
    async def test_cancel_task_not_found_returns_404(self, svc):
        """NOWAIT 探测 SELECT 返回空行 → 抛 NotFoundError。"""
        from app.core.exceptions import NotFoundError

        service, mock_session, mock_task_repo, mock_event_repo = svc

        # fetchone 返回 None → 任务不存在
        result = MagicMock()
        result.fetchone.return_value = None
        mock_session.execute = AsyncMock(return_value=result)

        with pytest.raises(NotFoundError):
            await service.cancel("task_missing_999", user_internal_id=42)

    @pytest.mark.asyncio
    async def test_cancel_user_mismatch_returns_404(self, svc):
        """任务存在但不属于当前用户 → NotFoundError（不泄露存在性）。"""
        from app.core.exceptions import NotFoundError

        service, mock_session, mock_task_repo, mock_event_repo = svc

        # NOWAIT SELECT 命中（拿到 id）
        result = MagicMock()
        result.fetchone.return_value = (137,)
        mock_session.execute = AsyncMock(return_value=result)

        # 但 get_by_public_id 返回 user_id=99，跟当前 user_internal_id=42 不符
        foreign_task = _make_task_row(task_id=137, user_id=99)
        mock_task_repo.get_by_public_id.return_value = foreign_task

        with pytest.raises(NotFoundError):
            await service.cancel("task_test_001", user_internal_id=42)

    @pytest.mark.asyncio
    async def test_cancel_normal_path_no_lock_conflict(self, svc):
        """happy path：orchestrator 没持锁 → NOWAIT SELECT 立即成功 → update
        status=cancelled → 写 task_cancelled event → commit 两次。"""
        service, mock_session, mock_task_repo, mock_event_repo = svc

        # 1) NOWAIT SELECT 命中
        result = MagicMock()
        result.fetchone.return_value = (137,)
        # 2) get_by_public_id 返回自己的任务
        own_task = _make_task_row(task_id=137, user_id=42, status="running")
        mock_task_repo.get_by_public_id.return_value = own_task
        mock_task_repo.update_status = AsyncMock()
        mock_event_repo.create = AsyncMock()

        mock_session.execute = AsyncMock(return_value=result)
        mock_session.commit = AsyncMock()

        with patch(
            "app.services.agent_task_service.utcnow",
            return_value=datetime(2026, 7, 14, 10, 0, 0),
        ), patch(
            "app.services.agent_task_service.generate_public_id",
            return_value="event_test_001",
        ):
            ret = await service.cancel("task_test_001", user_internal_id=42)

        assert ret == {"task_id": "task_test_001", "status": "cancelled"}

        # update_status 用 internal_id=137（不是 public_id）
        mock_task_repo.update_status.assert_called_once_with(137, "cancelled")

        # 写一条 task_cancelled event
        mock_event_repo.create.assert_called_once()
        event = mock_event_repo.create.call_args[0][0]
        assert event.event_type == "task_cancelled"
        assert event.task_id == 137

        # commit 两次：一次释放 cancel 的 SELECT 锁，一次提交 event
        assert mock_session.commit.await_count == 2


class TestSsePostConfirmRawSql:
    """SSE ``events-post-confirm`` 入口必须用 raw SQL 读 task 关键字段。

    生产事故：caller 的 ORM ``task`` 在 ``load_checkpoint → save_checkpoint → commit``
    之后被 expire；后续访问 ``task.id`` / ``task.task_context_json`` 触发 lazy load，
    撞 ``MissingGreenlet: greenlet_spawn has not been called``。

    修复：event_stream 入口立刻走 raw SQL 拿 ``id, conversation_id, task_context_json``，
    后续代码只用普通变量。本测试用 ast/static 检查守住这一约束，避免下次有人无意把
    SSE 端点改回 ``task_repo.get_by_public_id`` 触发同样事故。
    """

    def test_post_confirm_event_stream_avoids_orm_get_by_public_id(self):
        """``task_events_post_confirm_sse`` 的 event_stream 入口必须用 raw SQL，
        不再以 ``task = await task_repo.get_by_public_id(...)`` 模式起头
        （那是触发 MissingGreenlet 的根因：ORM 对象被 save_checkpoint commit expire）。

        检测要点：raw SQL SELECT 拿字段前不允许 await task_repo.get_by_public_id，
        后面（orchestrator 跑完后 commit 再 load 的 fresh 对象）是允许的。
        """
        import ast
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "api"
            / "v1"
            / "agent_tasks.py"
        )
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "task_events_post_confirm_sse":
                target_func = node
                break
        assert target_func is not None, "找不到 task_events_post_confirm_sse"

        # 找到 event_stream 函数并定位 raw SQL SELECT 的行号
        # raw SQL 之前的 task_repo.get_by_public_id 是被禁止的（caller 拿到的是
        # 会被 save_checkpoint expire 的对象）；raw SQL 之后允许（fresh load）。
        offenders: list[str] = []
        for inner in ast.walk(target_func):
            if isinstance(inner, ast.AsyncFunctionDef) and inner.name == "event_stream":
                # 用源码片段找 SELECT ... FROM agent_tasks 的行号
                src_segment = ast.get_source_segment(source, inner) or ""
                raw_sql_line = None
                for ln in src_segment.splitlines():
                    if "SELECT" in ln and "FROM agent_tasks" in ln:
                        # offset relative to whole file = inner.lineno + offset
                        # We'll fall back to scanning AST nodes by lineno < inner first await
                        break
                # 用 AST：找 event_stream 函数体内所有 await expr，按 lineno 排序，
                # 第一个 await task_repo.get_by_public_id 必须在 raw_sql_line 之后。
                # 简化：raw_sql_line 取 "session.execute" 调用第一次出现的行号。
                raw_sql_lineno = None
                for sub in ast.walk(inner):
                    if isinstance(sub, ast.Await) and isinstance(sub.value, ast.Call):
                        if (
                            isinstance(sub.value.func, ast.Attribute)
                            and sub.value.func.attr == "execute"
                            and sub.value.func.value.id == "session"
                        ):
                            raw_sql_lineno = sub.lineno
                            break
                # 现在检查 raw_sql_lineno 之前的 await task_repo.get_by_public_id
                for sub in ast.walk(inner):
                    if not isinstance(sub, ast.Await):
                        continue
                    v = sub.value
                    if not isinstance(v, ast.Call):
                        continue
                    func = v.func
                    if not (
                        isinstance(func, ast.Attribute)
                        and func.attr == "get_by_public_id"
                    ):
                        continue
                    base = func.value
                    while isinstance(base, ast.Attribute):
                        base = base.value
                    if isinstance(base, ast.Name) and base.id == "task_repo":
                        if raw_sql_lineno is None or sub.lineno < raw_sql_lineno:
                            offenders.append(
                                f"line {sub.lineno}: post-confirm SSE event_stream 入口 "
                                "(raw SQL SELECT 之前) 不允许 await task_repo.get_by_public_id(...); "
                                "否则 ORM task 在 save_checkpoint commit 后会 expire 并撞 MissingGreenlet。"
                            )
        assert not offenders, "\n".join(offenders)

    def test_post_confirm_sse_separates_owned_snapshot_from_event_replay(self):
        """Ownership is resolved before streaming; generator only replays events."""
        import ast
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "api"
            / "v1"
            / "agent_tasks.py"
        )
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "task_events_post_confirm_sse":
                target_func = node
                break
        assert target_func is not None

        endpoint_segment = ast.get_source_segment(source, target_func) or ""
        assert "get_owned_task(" in endpoint_segment
        assert endpoint_segment.index("get_owned_task(") < endpoint_segment.index(
            "async def event_stream"
        )

        found_event_select = False
        for inner in ast.walk(target_func):
            if isinstance(inner, ast.AsyncFunctionDef) and inner.name == "event_stream":
                src_segment = ast.get_source_segment(source, inner) or ""
                if (
                    "FROM agent_events" in src_segment
                    and "await s.execute" in src_segment
                ):
                    found_event_select = True
                    break
        assert found_event_select
