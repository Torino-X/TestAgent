"""Phase 2.9A.5 Resume Decision + InFlight + SSE cursor 修复测试。

覆盖:
  1. Producer source="user" 与 Coordinator 校验契约一致
  2. dispatch_resume 在所有异常路径释放 _inflight
  3. ValueError 不可重试
  4. SSE _replay_history 带 cursor + id: 行
  5. 前端 appendMessages 按 id 去重
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── A. Source 契约 ─────────────────────────────────────────────────────────


def test_producer_payload_source_is_user():
    """confirm_task producer payload 中 source 必须是 'user',不出现 user_confirm。

    Coordinator._validate_section_decision 只接受 'user' | 'timeout'。
    """
    from app.api.v1 import agent_tasks as module

    src = inspect.getsource(module.confirm_task)
    # 必须写 source="user"
    assert '"source": "user"' in src, "producer 必须写 source='user'"
    # 不能写 source="user_confirm"
    assert '"source": "user_confirm"' not in src
    # 必须修正 idempotency_key 与 source 对齐
    assert '|resume|user"' in src or '|resume|user' in src


def test_coordinator_rejects_user_confirm_source():
    """Coordinator._validate_section_decision 仍然拒绝 'user_confirm'。"""
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator

    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)
    # user_confirm 仍应被拒绝
    with pytest.raises(ValueError, match="section resume source invalid"):
        coord._validate_section_decision(
            {"kind": "section_confirmation", "sections": [], "source": "user_confirm"}
        )


# ── B. InFlight 完整释放 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_resume_releases_inflight_on_success():
    """dispatch_resume 成功路径必须释放 _inflight.end。"""
    from app.agent_runtime.api_dispatcher import ApiDispatcher

    inflight = MagicMock()
    inflight.begin = MagicMock()
    inflight.end = MagicMock()

    mock_coordinator = MagicMock()
    mock_coordinator.resume_section_confirmation = AsyncMock(return_value="ok")
    mock_orchestrator = MagicMock()
    mock_orchestrator.resume_after_confirm = AsyncMock()

    dispatcher = ApiDispatcher(
        coordinator=mock_coordinator,
        probe_report=MagicMock(
            production_dispatch_forced_off=False,
            postgres_ok=True,
            langgraph_readiness=True,
        ),
    )
    # 直接 patch 内部状态,绕开 _resolve_engine 的真实 feature flag 检查
    dispatcher._inflight = inflight
    dispatcher._redis_inflight = None
    dispatcher._feature_flags = MagicMock(
        production_dispatch_enabled=True,
        langgraph_enabled=True,
    )

    ctx = {
        "task_public_id": "task_xx",
        "task_internal_id": 100,
        "engine_type": "langgraph",
        "payload": {"source": "user", "sections": [{"section_id": "s1"}]},
        "sections": [{"section_id": "s1"}],
        "source": "user",
    }

    with patch.object(
        ApiDispatcher,
        "_resolve_engine",
        return_value=("langgraph", False, None),
    ):
        await dispatcher.dispatch_resume(
            task_public_id="task_xx",
            task_engine_type="langgraph",
            context=ctx,
        )

    inflight.begin.assert_called_once()
    inflight.end.assert_called_once_with("task_xx"), (
        "成功路径必须释放 in-flight,避免 Worker 重试撞 guard"
    )


@pytest.mark.asyncio
async def test_dispatch_resume_releases_inflight_on_value_error():
    """dispatch_resume 抛 ValueError 时必须释放 _inflight.end。"""
    from app.agent_runtime.api_dispatcher import ApiDispatcher

    inflight = MagicMock()
    inflight.begin = MagicMock()
    inflight.end = MagicMock()

    mock_coordinator = MagicMock()
    mock_coordinator.resume_section_confirmation = AsyncMock(
        side_effect=ValueError("section resume source invalid: 'user_confirm'")
    )
    mock_orchestrator = MagicMock()
    mock_orchestrator.resume_after_confirm = AsyncMock()

    dispatcher = ApiDispatcher(
        coordinator=mock_coordinator,
        probe_report=MagicMock(
            production_dispatch_forced_off=False,
            postgres_ok=True,
            langgraph_readiness=True,
        ),
    )
    dispatcher._inflight = inflight
    dispatcher._redis_inflight = None
    dispatcher._feature_flags = MagicMock(
        production_dispatch_enabled=True,
        langgraph_enabled=True,
    )

    ctx = {
        "task_public_id": "task_v",
        "task_internal_id": 100,
        "engine_type": "langgraph",
        "payload": {"source": "user"},
        "sections": [{"section_id": "s1"}],
        "source": "user",
    }

    with patch.object(
        ApiDispatcher,
        "_resolve_engine",
        return_value=("langgraph", False, None),
    ):
        with pytest.raises(ValueError):
            await dispatcher.dispatch_resume(
                task_public_id="task_v",
                task_engine_type="langgraph",
                context=ctx,
            )

    inflight.end.assert_called_once_with("task_v")


@pytest.mark.asyncio
async def test_dispatch_resume_releases_inflight_on_type_error():
    """dispatch_resume 抛 TypeError 时也必须释放 _inflight。"""
    from app.agent_runtime.api_dispatcher import ApiDispatcher

    inflight = MagicMock()
    inflight.end = MagicMock()

    mock_coordinator = MagicMock()
    mock_coordinator.resume_section_confirmation = AsyncMock(
        side_effect=TypeError("unexpected keyword argument 'task_id'")
    )
    mock_orchestrator = MagicMock()
    mock_orchestrator.resume_after_confirm = AsyncMock()

    dispatcher = ApiDispatcher(
        coordinator=mock_coordinator,
        probe_report=MagicMock(
            production_dispatch_forced_off=False,
            postgres_ok=True,
            langgraph_readiness=True,
        ),
    )
    dispatcher._inflight = inflight
    dispatcher._redis_inflight = None
    dispatcher._feature_flags = MagicMock(
        production_dispatch_enabled=True,
        langgraph_enabled=True,
    )

    ctx = {
        "task_public_id": "task_t",
        "task_internal_id": 100,
        "engine_type": "langgraph",
        "payload": {"source": "user"},
        "sections": [],
        "source": "user",
    }

    with patch.object(
        ApiDispatcher,
        "_resolve_engine",
        return_value=("langgraph", False, None),
    ):
        with pytest.raises(TypeError):
            await dispatcher.dispatch_resume(
                task_public_id="task_t",
                task_engine_type="langgraph",
                context=ctx,
            )

    inflight.end.assert_called_once_with("task_t")


# ── C. ValueError 不可重试 ───────────────────────────────────────────────


def test_value_error_in_non_retryable():
    """Worker _NON_RETRYABLE 必须包含 ValueError。"""
    from app.services import agent_execution_worker

    src = inspect.getsource(agent_execution_worker)
    assert "ValueError" in src
    # 检查 _NON_RETRYABLE tuple 包含 ValueError
    import re

    m = re.search(r"_NON_RETRYABLE\s*=\s*\(([^)]+)\)", src)
    assert m, "必须定义 _NON_RETRYABLE"
    tuple_content = m.group(1)
    assert "ValueError" in tuple_content, (
        f"ValueError 必须加入 _NON_RETRYABLE,当前 tuple:{tuple_content}"
    )


# ── D. SSE cursor + id: 行 ──────────────────────────────────────────────


def test_replay_history_sql_has_cursor():
    """_replay_history SQL 必须带 AND sequence_no > :after_seq cursor。"""
    from app.api.v1 import agent_tasks as module

    src = inspect.getsource(module.task_events_post_confirm_sse)
    assert "sequence_no > :after_seq" in src, (
        "_replay_history 必须用 cursor 过滤,防止 pre-confirm 已消费事件被重放"
    )


def test_sse_event_yields_id_line():
    """每个 yield 必须包含 id: <sequence_no>\\n 行。"""
    from app.api.v1 import agent_tasks as module

    src = inspect.getsource(module.task_events_post_confirm_sse)
    # 历史回放 yield 必须含 'id: {seq_no}\n'
    assert 'f"id: {seq_no}\\n"' in src or 'f"id: {seq_no}\n"' in src, (
        "历史事件 yield 必须含 id 行供前端 cursor"
    )
    # 实时事件 yield 也必须含 id
    assert 'f"{id_line}event:' in src or 'id_line' in src


# ── E. 前端 appendMessages 去重 ────────────────────────────────────────────


def test_conversation_store_append_messages_dedupe():
    """conversationStore.appendMessages 必须按 id 去重。"""
    import os
    import re
    frontend_path = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..", "frontend",
            "src", "stores", "conversationStore.ts",
        )
    )
    with open(frontend_path, encoding="utf-8") as f:
        content = f.read()
    # 检查 appendMessages 内部有 existingIds / Set
    assert "existingIds" in content or "new Set" in content, (
        "appendMessages 必须实现 id 去重(用 Set)"
    )


def test_append_messages_filters_existing_ids():
    """appendMessages 实现必须 filter 出已存在的 id。"""
    import os
    import re
    frontend_path = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..", "frontend",
            "src", "stores", "conversationStore.ts",
        )
    )
    with open(frontend_path, encoding="utf-8") as f:
        content = f.read()
    # 找 appendMessages 实现块
    m = re.search(
        r"function appendMessages\([^)]*\)\s*\{[^}]+filter[^}]+\}",
        content,
        re.DOTALL,
    )
    assert m, "appendMessages 必须有 filter 调用"
    body = m.group(0)
    assert "existingIds" in body, (
        "filter 内部必须基于 existingIds / Set.has"
    )
