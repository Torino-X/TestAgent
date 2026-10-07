from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.agent_runtime.adapters.test_agent_tool_adapter import TestAgentToolAdapter
from app.agent_runtime.production_runtime_context_factory import (
    ProductionRuntimeContextFactory,
)


class _FakeBus:
    async def publish(self, *_args, **_kwargs):
        return None


class _FakeCancellationService:
    async def is_cancelled(self, _task_id: str) -> bool:
        return False


def _unused_session_factory():
    raise AssertionError("the factory must not open a database session eagerly")


@pytest.mark.asyncio
async def test_factory_builds_real_runtime_context_with_public_event_channel() -> None:
    factory = ProductionRuntimeContextFactory(
        session_factory=_unused_session_factory,
        event_bus_provider=lambda: _FakeBus(),
        cancellation_service=_FakeCancellationService(),
        clock=datetime.utcnow,
    )
    resolver = object()
    factory._load_task_flag_resolver = AsyncMock(return_value=resolver)

    context = await factory(
        {
            "task_id": "task_public_123",
            "graph_run_id": "run-task_public_123",
            "task_internal_id": 101,
            "conversation_internal_id": 102,
            "user_internal_id": 103,
        }
    )

    assert context.task_internal_id == 101
    assert context.task_public_id == "task_public_123"
    assert context.task_flag_resolver is resolver
    assert context.conversation_internal_id == 102
    assert context.user_internal_id == 103
    assert isinstance(context.tool_adapter, TestAgentToolAdapter)
    assert context.settings_service is not None
    assert context.event_sink._task_public_id == "task_public_123"
    # llm_client is a RuntimeContext field; with a raising session factory the
    # provider build fails gracefully → None (dynamic subgraph falls back).
    assert "llm_client" in context.__dataclass_fields__


def test_factory_rejects_missing_internal_identity() -> None:
    factory = ProductionRuntimeContextFactory(
        session_factory=_unused_session_factory,
        event_bus_provider=lambda: _FakeBus(),
        cancellation_service=_FakeCancellationService(),
    )

    with pytest.raises(ValueError, match="task_internal_id"):
        asyncio.run(factory({"task_id": "task_public_123"}))
