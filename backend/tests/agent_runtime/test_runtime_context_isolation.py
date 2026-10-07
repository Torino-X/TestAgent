"""RuntimeContext 与 state 隔离 - 不允许 Session/Client 字段进入 state。"""

from __future__ import annotations

from datetime import datetime
from typing import AsyncIterator

import pytest

from app.agent_runtime.cancellation import InMemoryCancellationService
from app.agent_runtime.events.sink import InMemoryEventSink
from app.agent_runtime.graphs.test_plan.state import (
    assert_state_serializable,
    make_empty_state,
)
from app.agent_runtime.runtime_context import RuntimeContext, RuntimeContextFactory


class _FakeSettingsService:
    """最小占位 - 不调用任何方法。"""

    def __getattr__(self, name: str):  # pragma: no cover - 仅类型占位
        raise AttributeError(name)


def _session_factory() -> AsyncIterator[None]:
    async def _ctx():  # pragma: no cover - 不会真的调用
        yield None

    return _ctx()


def test_runtime_context_is_frozen() -> None:
    sink = InMemoryEventSink()
    cancel = InMemoryCancellationService()
    ctx = RuntimeContext(
        user_internal_id=1,
        task_internal_id=2,
        conversation_internal_id=3,
        session_factory=_session_factory,
        settings_service=_FakeSettingsService(),
        event_sink=sink,
        cancellation_service=cancel,
    )
    with pytest.raises(Exception):
        ctx.user_internal_id = 999  # type: ignore[misc]


def test_runtime_context_serializable_check_passes_for_state() -> None:
    """确保 RuntimeContext 不被错误塞进 state。"""
    state = make_empty_state(task_id="t-1", graph_run_id="r-1")
    # state 中不应出现 runtime context 的字段
    assert "runtime_context" not in state
    assert "session_factory" not in state
    assert "event_sink" not in state
    assert_state_serializable(dict(state))


def test_factory_build_produces_independent_ctx() -> None:
    factory = RuntimeContextFactory(
        user_internal_id=1,
        task_internal_id=2,
        conversation_internal_id=3,
        session_factory=_session_factory,
        settings_service=_FakeSettingsService(),
        event_sink=InMemoryEventSink(),
        cancellation_service=InMemoryCancellationService(),
        clock=lambda: datetime(2026, 1, 1),
    )
    ctx_a = factory.build()
    ctx_b = factory.build()
    assert ctx_a is not ctx_b
    assert ctx_a.user_internal_id == ctx_b.user_internal_id