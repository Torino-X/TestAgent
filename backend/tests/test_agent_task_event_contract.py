from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_event_list_summary_exposes_replay_contract_fields() -> None:
    from app.services.agent_task_service import AgentTaskService

    event = SimpleNamespace(
        public_id="event_001",
        event_type="tool_finished",
        message_type="tool_call",
        title="RequirementParserTool finished",
        content="parsed requirement",
        payload_json={"tool_call_id": "call_001"},
        status="completed",
        sequence_no=12,
        graph_run_id="run_001",
        graph_version="v2",
        node_name="requirement_parsed",
        event_schema_version=2,
        created_at=None,
    )

    summary = AgentTaskService._event_to_summary(event)

    assert summary["message_type"] == "tool_call"
    assert summary["status"] == "completed"
    assert summary["sequence_no"] == 12
    assert summary["graph_run_id"] == "run_001"
    assert summary["node_name"] == "requirement_parsed"


@pytest.mark.asyncio
async def test_event_list_attaches_its_public_task_id() -> None:
    from app.services.agent_task_service import AgentTaskService

    event = SimpleNamespace(
        public_id="event_001",
        event_type="tool_finished",
        message_type="tool_call",
        title="RequirementParserTool finished",
        content="parsed requirement",
        payload_json={},
        status="completed",
        sequence_no=12,
        graph_run_id="run_001",
        graph_version="v2",
        node_name="requirement_parsed",
        event_schema_version=2,
        created_at=None,
    )
    service = AgentTaskService.__new__(AgentTaskService)
    service._task_repo = SimpleNamespace(
        get_by_public_id=AsyncMock(return_value=SimpleNamespace(id=7, user_id=11))
    )
    # Phase 2.9A.27: list_by_task returns (events, next_cursor, total) when
    # return_total=True; the service unwraps it into the public ``list_events``
    # payload shape.
    service._event_repo = SimpleNamespace(
        list_by_task=AsyncMock(return_value=([event], None, 1))
    )

    payload = await service.list_events("task_public_001", 11)

    assert payload["total"] == 1
    assert payload["next_cursor"] is None
    assert payload["events"][0]["task_id"] == "task_public_001"
