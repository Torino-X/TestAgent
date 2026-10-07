"""LangGraph stub 图 astream 行为。"""

from __future__ import annotations

import pytest

from app.agent_runtime.graph_runtime_service import GraphRuntimeService
from app.agent_runtime.graphs.test_plan.state import make_empty_state


@pytest.mark.asyncio
async def test_astream_yields_at_least_one_chunk() -> None:
    service = GraphRuntimeService.build_default(checkpointer=None)
    state = make_empty_state(task_id="t-1", graph_run_id="r-1")
    chunks = []
    async for chunk in service.astream(state):
        chunks.append(chunk)
    assert len(chunks) >= 1
    final = chunks[-1]
    assert final["current_node"] == "initialize_stub"