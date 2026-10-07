"""Phase 2.9A.30: trigger_message_id public_id tests.

Tests for resolve_trigger_message_id returning public_id (string) instead of internal int.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _RowLike:
    """Minimal SQLAlchemy Row substitute supporting r[i] indexing."""

    def __init__(self, values: tuple):
        self._values = values

    def __getitem__(self, key):
        return self._values[key]


def _row(*values):
    return _RowLike(values)


@pytest.mark.asyncio
async def test_resolve_returns_internal_and_public_id():
    """When trigger_message_id is set, should query public_id and return tuple."""
    from app.services.agent_task_service import AgentTaskService

    task = SimpleNamespace(
        id=70,
        conversation_id=91,
        trigger_message_id=123,
        created_at=None,
    )

    fake_row = _row("msg_pub_123")
    fake_result = SimpleNamespace(first=lambda: fake_row)
    fake_session = SimpleNamespace(execute=AsyncMock(return_value=fake_result))

    service = AgentTaskService.__new__(AgentTaskService)
    service._session = fake_session

    internal_id, public_id = await service.resolve_trigger_message_id(task)

    assert internal_id == 123
    assert public_id == "msg_pub_123"


@pytest.mark.asyncio
async def test_resolve_legacy_fallback_returns_tuple():
    """Legacy fallback (trigger_message_id=None) should return (internal_id, public_id)."""
    from app.services.agent_task_service import AgentTaskService

    task = SimpleNamespace(
        id=70,
        conversation_id=91,
        trigger_message_id=None,
        created_at=None,
    )

    fake_row = _row(94, "msg_trigger_94")
    fake_result = SimpleNamespace(first=lambda: fake_row)
    fake_session = SimpleNamespace(execute=AsyncMock(return_value=fake_result))

    service = AgentTaskService.__new__(AgentTaskService)
    service._session = fake_session

    internal_id, public_id = await service.resolve_trigger_message_id(task)

    assert internal_id == 94
    assert public_id == "msg_trigger_94"


@pytest.mark.asyncio
async def test_resolve_no_match_returns_none_tuple():
    """When no matching message found, should return (None, None)."""
    from app.services.agent_task_service import AgentTaskService

    task = SimpleNamespace(
        id=70,
        conversation_id=91,
        trigger_message_id=None,
        created_at=None,
    )

    fake_result = SimpleNamespace(first=lambda: None)
    fake_session = SimpleNamespace(execute=AsyncMock(return_value=fake_result))

    service = AgentTaskService.__new__(AgentTaskService)
    service._session = fake_session

    result = await service.resolve_trigger_message_id(task)
    assert result == (None, None)


@pytest.mark.asyncio
async def test_resolve_db_error_returns_none_tuple():
    """DB exception should return (None, None) gracefully."""
    from app.services.agent_task_service import AgentTaskService

    task = SimpleNamespace(
        id=70,
        conversation_id=91,
        trigger_message_id=None,
        created_at=None,
    )

    fake_session = SimpleNamespace(
        execute=AsyncMock(side_effect=Exception("connection reset"))
    )

    service = AgentTaskService.__new__(AgentTaskService)
    service._session = fake_session

    result = await service.resolve_trigger_message_id(task)
    assert result == (None, None)


def test_to_detail_trigger_is_string():
    """_to_detail should accept trigger_message_id as string."""
    from app.services.agent_task_service import AgentTaskService

    task = SimpleNamespace(
        public_id="task_001",
        task_type="test_plan_generation",
        status="completed",
        plan_json=None,
        task_context_json=None,
        review_result_json=None,
        active_run_id=None,
        runtime_status="completed",
        started_at=None,
        completed_at=None,
        trigger_message_id=None,
    )
    out = AgentTaskService._to_detail(
        task, trigger_message_id="msg_d30af2c4"
    )
    assert out["trigger_message_id"] == "msg_d30af2c4"
    assert isinstance(out["trigger_message_id"], str)


def test_to_detail_trigger_none_when_not_provided():
    """_to_detail should return None when trigger_message_id not provided."""
    from app.services.agent_task_service import AgentTaskService

    task = SimpleNamespace(
        public_id="task_001",
        task_type="test_plan_generation",
        status="completed",
        plan_json=None,
        task_context_json=None,
        review_result_json=None,
        active_run_id=None,
        runtime_status="completed",
        started_at=None,
        completed_at=None,
        trigger_message_id=None,
    )
    out = AgentTaskService._to_detail(task)
    assert out["trigger_message_id"] is None
