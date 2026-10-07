from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.agent_task_service import AgentTaskService


def _task(*, active_run_id: str | None = "run_public_1"):
    return SimpleNamespace(
        id=17,
        public_id="task_public_1",
        user_id=42,
        task_type="test_plan_generation",
        status="completed",
        plan_json=[{"step": "export"}],
        task_context_json={"conversation_id": "conv_1"},
        review_result_json=None,
        active_run_id=active_run_id,
        runtime_status="completed",
        started_at=None,
        completed_at=None,
    )


@pytest.mark.asyncio
async def test_get_task_looks_up_active_run_by_public_id_and_serializes_timing():
    service = AgentTaskService(AsyncMock())
    service._task_repo = AsyncMock()
    service._run_repo = AsyncMock()

    started_at = datetime(2026, 7, 24, 1, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 7, 24, 1, 0, 2, 500000, tzinfo=timezone.utc)
    run = SimpleNamespace(
        public_id="run_public_1",
        status="completed",
        started_at=started_at,
        finished_at=finished_at,
    )
    service._task_repo.get_by_public_id.return_value = _task()
    service._run_repo.get_by_public_id.return_value = run

    detail = await service.get_task("task_public_1", user_internal_id=42)

    service._run_repo.get_by_public_id.assert_awaited_once_with("run_public_1")
    service._run_repo.get_by_internal_id.assert_not_awaited()
    assert detail["active_run_id"] == "run_public_1"
    assert detail["started_at"] == started_at.isoformat()
    assert detail["completed_at"] == finished_at.isoformat()
    assert detail["duration_ms"] == 2500
    assert detail["run"]["duration_ms"] == 2500


@pytest.mark.asyncio
async def test_get_task_falls_back_to_latest_run_when_active_run_is_missing():
    service = AgentTaskService(AsyncMock())
    service._task_repo = AsyncMock()
    service._run_repo = AsyncMock()

    latest_run = SimpleNamespace(
        public_id="run_latest",
        status="failed",
        started_at=datetime(2026, 7, 24, 1, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 7, 24, 1, 1, tzinfo=timezone.utc),
    )
    service._task_repo.get_by_public_id.return_value = _task(active_run_id=None)
    service._run_repo.list_by_task.return_value = [latest_run]

    detail = await service.get_task("task_public_1", user_internal_id=42)

    service._run_repo.list_by_task.assert_awaited_once_with(17, limit=1)
    assert detail["active_run_id"] == "run_latest"
    assert detail["run"]["status"] == "failed"
    assert detail["duration_ms"] == 60000


@pytest.mark.asyncio
async def test_pending_preparation_clarification_exposes_persisted_cards():
    """The pending-confirmation API must restore cards after the SSE event."""
    service = AgentTaskService(AsyncMock())
    service._task_repo = AsyncMock()
    service._confirm_repo = AsyncMock()
    service._task_repo.get_by_public_id.return_value = _task()
    service._confirm_repo.get_pending_by_task.return_value = SimpleNamespace(
        public_id="confirmation_clarification_1",
        confirmation_type="preparation_clarification",
        status="pending",
        request_json={
            "cards": [{"id": "release_scope", "question": "Choose scope"}],
            "retrieval_summary": {"company_knowledge": "skipped"},
        },
    )

    pending = await service.get_pending_confirmation("task_public_1", 42)

    assert pending is not None
    assert pending["confirmation_type"] == "preparation_clarification"
    assert pending["cards"] == [
        {"id": "release_scope", "question": "Choose scope"}
    ]
    assert pending["retrieval_summary"] == {"company_knowledge": "skipped"}
