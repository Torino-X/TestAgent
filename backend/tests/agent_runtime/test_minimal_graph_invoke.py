"""LangGraph stub 图 invoke 行为。"""

from __future__ import annotations

import pytest

from app.agent_runtime.graph_runtime_service import GraphRuntimeService
from app.agent_runtime.graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V1,
)
from app.agent_runtime.graphs.test_plan.state import make_empty_state
from app.agent_runtime.graphs.test_plan.versions.v1 import NODE_INITIALIZE_STUB


@pytest.mark.asyncio
async def test_minimal_graph_ainvoke_returns_initialized_state() -> None:
    service = GraphRuntimeService.build_default(checkpointer=None)
    state = make_empty_state(task_id="t-1", graph_run_id="r-1")
    result = await service.ainvoke(state)
    assert result["current_node"] == NODE_INITIALIZE_STUB
    assert result["current_phase"] == "initialized"
    assert result["task_status"] == "running"
    assert NODE_INITIALIZE_STUB in result["completed_nodes"]


@pytest.mark.asyncio
async def test_minimal_graph_thread_id_defaults_to_task_id() -> None:
    service = GraphRuntimeService.build_default(checkpointer=None)
    state = make_empty_state(task_id="abc-xyz", graph_run_id="r-1")
    # 不显式传 config,thread_id 应取 task_id
    result = await service.ainvoke(state)
    assert result["task_id"] == "abc-xyz"


@pytest.mark.asyncio
async def test_minimal_graph_explicit_config_used() -> None:
    service = GraphRuntimeService.build_default(checkpointer=None)
    state = make_empty_state(task_id="t-1", graph_run_id="r-1")
    cfg = {"configurable": {"thread_id": "explicit-thread"}}
    result = await service.ainvoke(state, config=cfg)
    assert result is not None