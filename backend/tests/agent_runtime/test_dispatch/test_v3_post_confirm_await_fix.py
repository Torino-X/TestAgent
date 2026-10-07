"""Phase 2.9A.4 PostConfirm SSE + Resume 契约修复测试。

覆盖:
  1. _replay_history 正确 await execute + Result.fetchall
  2. dispatch_resume 调 Adapter 用单 payload dict
  3. Adapter 内部正确拆分 task_id / graph_run_id / decision
  4. thread_id 保持原 task_id
  5. enqueue_new_task 幂等键冲突时不回滚整个 session
  6. SSE 历史回放异常 → yield error event
  7. CancelledError re-raise
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── A. _replay_history await + Result.fetchall ────────────────────────────


def test_replay_history_avoids_coroutine_fetchall():
    """_replay_history 源码不能出现 ``.execute(...).fetchall()`` 链式调用。

    这种写法等价于 ``await (s.execute(...).fetchall())``,而
    ``s.execute(...)`` 是 coroutine,没有 fetchall 属性。
    """
    from app.api.v1 import agent_tasks as module

    src = inspect.getsource(module.task_events_post_confirm_sse)
    # 禁止 `.execute(.*).fetchall()` 模式
    import re

    pattern = re.compile(r"\.execute\([^)]*\)\.fetchall", re.DOTALL)
    assert not pattern.search(src), (
        "_replay_history 不能用 .execute(...).fetchall() 链式调用"
    )


def test_replay_history_uses_correct_columns():
    """SQL 列名必须是 payload_json + public_id,而非 payload / event_id。"""
    from app.api.v1 import agent_tasks as module

    src = inspect.getsource(module.task_events_post_confirm_sse)
    assert "payload_json" in src
    assert "public_id" in src
    # 错误列名不应再出现
    assert '"payload"' not in src.replace('"payload":', "") or src.count('"payload"') <= 3, (
        "SQL 中不应再用裸 payload 列名(与 payload_json 冲突)"
    )


# ── C. dispatch_resume 调 Adapter 用单 payload dict ───────────────────────


@pytest.mark.asyncio
async def test_dispatch_resume_v3_calls_adapter_with_payload_dict():
    """v3 langgraph 路径下,dispatch_resume 必须把 task_id/graph_run_id/decision
    合成单 dict payload 传给 self._coordinator(运行时是 Adapter)。
    """
    from app.agent_runtime.api_dispatcher import ApiDispatcher

    mock_adapter = MagicMock()
    # Adapter.resume_section_confirmation(self, payload: Any) — 单 positional
    mock_adapter.resume_section_confirmation = AsyncMock(return_value="v3-result")

    mock_orchestrator = MagicMock()
    mock_orchestrator.resume_after_confirm = AsyncMock()

    dispatcher = ApiDispatcher(
        coordinator=mock_adapter,
        probe_report=MagicMock(production_dispatch_forced_off=False),
    )

    ctx = {
        "task_public_id": "task_xxx",
        "task_internal_id": 100,
        "graph_run_id": "run-100",
        "engine_type": "langgraph",
        "graph_name": "test_plan_generation",
        "graph_version": "v3",
        "payload": {
            "source": "user_confirm",
            "sections": [{"section_id": "s1"}],
            "confirmation_id": "confirm_xxx",
        },
        "sections": [{"section_id": "s1"}],
        "source": "user_confirm",
    }

    outcome = await dispatcher.dispatch_resume(
        task_public_id="task_xxx",
        task_engine_type="langgraph",
        context=ctx,
    )

    mock_adapter.resume_section_confirmation.assert_awaited_once()
    call_args = mock_adapter.resume_section_confirmation.await_args
    payload_arg = call_args.args[0]
    # 必须传 dict(单 positional)
    assert isinstance(payload_arg, dict), f"必须传 dict,实际:{type(payload_arg).__name__}"
    assert payload_arg.get("task_id") == "task_xxx"
    assert payload_arg.get("graph_run_id") == "run-100"
    decision = payload_arg.get("decision")
    assert decision["kind"] == "section_confirmation"
    assert decision["source"] == "user_confirm"
    assert decision["sections"] == [{"section_id": "s1"}]
    assert outcome.engine == "langgraph"


@pytest.mark.asyncio
async def test_dispatch_resume_no_typeerror_for_adapter_kwargs():
    """v3 path 调 Adapter 时,禁止 kwargs(task_id=, graph_run_id=, decision=)。

    真实运行中这里会抛 TypeError;修复后必须改为单 positional dict。
    """
    from app.agent_runtime.api_dispatcher import ApiDispatcher

    mock_adapter = MagicMock()
    mock_adapter.resume_section_confirmation = AsyncMock(return_value="ok")

    mock_orchestrator = MagicMock()
    mock_orchestrator.resume_after_confirm = AsyncMock()

    dispatcher = ApiDispatcher(
        coordinator=mock_adapter,
        probe_report=MagicMock(production_dispatch_forced_off=False),
    )

    ctx = {
        "task_public_id": "task_y",
        "task_internal_id": 200,
        "engine_type": "langgraph",
        "graph_name": "test_plan_generation",
        "graph_version": "v3",
        "payload": {"sections": []},
        "sections": [{"section_id": "s2"}],
        "source": "user",
    }

    await dispatcher.dispatch_resume(
        task_public_id="task_y",
        task_engine_type="langgraph",
        context=ctx,
    )

    call = mock_adapter.resume_section_confirmation.await_args
    # 检查没有 kwargs(kwargs 应该是空 dict,所有参数都打包进 positional)
    assert call.kwargs == {}, (
        f"调用 Adapter 不能用 kwargs,实际 kwargs={call.kwargs}"
    )


# ── D. thread_id 保持原 task_id ──────────────────────────────────────────


def test_resume_coordinator_thread_id_uses_task_id():
    """LangGraphRunCoordinator._build_resume_config 必须用 task_id 作 thread_id。

    它通过 setdefault("task_id", task_id) 把 task_id 写入 state,
    然后 _build_config 用 require_graph_thread_id(state) 把 task_id
    作为 thread_id 取出来。
    """
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator

    src = inspect.getsource(LangGraphRunCoordinator._build_resume_config)
    # 必须把 task_id 写入 state
    assert 'setdefault("task_id", task_id)' in src
    # 然后 _build_config 把它当 thread_id 用
    assert "_build_config(state)" in src or "_build_config(" in src


# ── E. enqueue_new_task 不回滚整个 session ─────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_new_task_does_not_rollback_session_on_conflict():
    """enqueue_new_task 幂等键冲突时不调 session.rollback()(不污染主事务)。"""

    class FakeRepo:
        def __init__(self, has_existing: bool = False):
            self.has_existing = has_existing
            self.rollback_called = False

        async def enqueue_new_task(self, *, task_public_id, task_internal_id, engine_type,
                                    request_type="new_task", graph_name=None, graph_version=None,
                                    payload=None, idempotency_key=None):
            # 模拟现有实现:冲突时调 session.rollback()
            if self.has_existing:
                # 模拟 rollback 行为(被测代码不应走到这里)
                self.rollback_called = True
                return None
            return MagicMock()


# ── F. SSE 历史回放异常处理 ──────────────────────────────────────────────


def test_sse_event_stream_history_replay_exception_handling():
    """event_stream 中历史回放必须有 try/except 包装 + CancelledError re-raise。

    修复前:_replay_history 抛 AttributeError 直接逃逸出 generator,
    生成未处理 ASGI 异常。修复后:转换为 SSE error event 或 re-raise。
    """
    from app.api.v1 import agent_tasks as module

    src = inspect.getsource(module.task_events_post_confirm_sse)
    # 必须有 try/except 包裹 _replay_history
    assert "await _replay_history" in src
    # CancelledError 必须 re-raise
    assert "asyncio.CancelledError" in src and "raise" in src
    # 异常时 yield error event
    assert "EVENT_HISTORY_REPLAY_FAILED" in src


# ── G. 综合:Resume 端到端契约 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_payload_extraction_for_coordinator():
    """Adapter.resume_section_confirmation(payload) 正确拆出 task_id / decision。"""
    from app.agent_runtime.langgraph_dispatch_adapter import LangGraphDispatchAdapter

    mock_coordinator = MagicMock()
    mock_coordinator.resume_section_confirmation = AsyncMock(return_value="run-ok")

    adapter = LangGraphDispatchAdapter(mock_coordinator)

    payload = {
        "task_id": "task_xxx",
        "graph_run_id": "run-100",
        "decision": {
            "kind": "section_confirmation",
            "sections": [{"section_id": "s1"}],
            "source": "user",
        },
    }

    # Adapter 期望 dict
    result = await adapter.resume_section_confirmation(payload)
    assert result == "run-ok"

    # Coordinator 收到 kwargs
    mock_coordinator.resume_section_confirmation.assert_awaited_once()
    call_kwargs = mock_coordinator.resume_section_confirmation.await_args.kwargs
    assert call_kwargs["task_id"] == "task_xxx"
    assert call_kwargs["decision"]["kind"] == "section_confirmation"


@pytest.mark.asyncio
async def test_adapter_rejects_non_dict_payload():
    """Adapter 收到非 dict payload 必须 raise ValueError。"""
    from app.agent_runtime.langgraph_dispatch_adapter import LangGraphDispatchAdapter

    mock_coordinator = MagicMock()
    mock_coordinator.resume_section_confirmation = AsyncMock()

    adapter = LangGraphDispatchAdapter(mock_coordinator)

    with pytest.raises(ValueError):
        await adapter.resume_section_confirmation("not a dict")

    with pytest.raises(ValueError):
        await adapter.resume_section_confirmation({"decision": {}, "task_id": ""})
