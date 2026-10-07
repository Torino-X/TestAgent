"""Immutable Project identity propagation across task and artifact lifecycles."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.agent_task import AgentTask
from app.core.exceptions import UnsupportedLegacyTaskError
from app.services.agent_task_service import AgentTaskService
from app.services.artifact_writer import resolve_task_project_id


@pytest.mark.asyncio
async def test_retry_inherits_original_task_project_id(monkeypatch):
    service = AgentTaskService(MagicMock())
    old_task = SimpleNamespace(
        id=9,
        public_id="task_old",
        user_id=1,
        conversation_id=2,
        project_id=3,
        task_type="test_plan_generation",
        status="completed",
        title="方案",
        user_instruction="生成",
        plan_json=None,
        task_context_json=None,
        latest_snapshot_id=None,
        engine_type="langgraph",
        graph_name="test_plan",
        graph_version="v3",
    )
    service._task_repo.get_by_public_id = AsyncMock(return_value=old_task)
    captured = {}

    async def create(task):
        captured["task"] = task
        task.id = 10
        return task

    service._task_repo.create = create
    service._event_repo.create = AsyncMock()
    monkeypatch.setattr(
        "app.repositories.agent_execution_request_repository.AgentExecutionRequestRepository.enqueue_new_task",
        AsyncMock(return_value=SimpleNamespace(id=1)),
    )

    await service.retry("task_old", "from_beginning", 1)

    assert captured["task"].project_id == 3


@pytest.mark.asyncio
async def test_retry_rejects_every_historical_graph_version():
    service = AgentTaskService(MagicMock())
    old_task = SimpleNamespace(
        public_id="task_dynamic_v1",
        user_id=1,
        engine_type="langgraph",
        graph_name="dynamic_agent",
        graph_version="v1",
    )
    service._task_repo.get_by_public_id = AsyncMock(return_value=old_task)
    service._task_repo.create = AsyncMock()

    with pytest.raises(UnsupportedLegacyTaskError):
        await service.retry("task_dynamic_v1", "from_beginning", 1)

    service._task_repo.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_artifact_project_resolution_reads_immutable_task_fk(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        task = AgentTask(
            id=10,
            public_id="task_project_owner",
            user_id=1,
            conversation_id=2,
            project_id=3,
            task_type="test_plan_generation",
            status="created",
            created_at=datetime(2026, 9, 8, 10, 0, 0),
            updated_at=datetime(2026, 9, 8, 10, 0, 0),
        )
        session.add(task)
        await session.flush()

        assert await resolve_task_project_id(session, task.id) == 3
        assert await resolve_task_project_id(None, task.id) is None
