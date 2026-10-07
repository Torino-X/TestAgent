"""Repository-level engine identity invariants after Legacy retirement."""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from app.repositories.agent_task_repository import AgentTaskRepository


def test_repository_exposes_no_engine_mutation_or_legacy_normalizer() -> None:
    assert not hasattr(AgentTaskRepository, "update_engine_type_admin")
    assert not hasattr(AgentTaskRepository, "as_legacy_engine")


def test_runtime_update_signature_cannot_accept_engine_type() -> None:
    params = inspect.signature(AgentTaskRepository.update_engine_fields).parameters
    assert "engine_type" not in params


def test_create_requires_explicit_langgraph_engine_in_source() -> None:
    source = inspect.getsource(AgentTaskRepository.create)
    assert 'engine_type != "langgraph"' in source
    assert "new AgentTask rows require engine_type='langgraph'" in source
    assert '"et": engine_type' in source


@pytest.mark.asyncio
async def test_update_status_never_writes_engine_type() -> None:
    session = AsyncMock()
    repo = AgentTaskRepository(session)
    await repo.update_status(42, "running")
    statement, params = session.execute.await_args.args
    sql = str(statement).lower()
    assert "set status" in sql
    assert "engine_type" not in sql
    assert params == {"st": "running", "tid": 42}


@pytest.mark.asyncio
async def test_update_engine_fields_never_writes_engine_type() -> None:
    session = AsyncMock()
    repo = AgentTaskRepository(session)
    await repo.update_engine_fields(
        42,
        graph_name="test_plan_generation",
        graph_version="v3",
        runtime_status="running",
    )
    statement, params = session.execute.await_args.args
    sql = str(statement).lower()
    assert "engine_type" not in sql
    assert params["graph_version"] == "v3"
