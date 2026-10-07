"""MemorySaver 线程恢复 - 同一个 thread_id 二次 ainvoke 累积 state。"""

from __future__ import annotations

import pytest

from app.agent_runtime.graph_runtime_service import GraphRuntimeService
from app.agent_runtime.graphs.test_plan.state import make_empty_state


@pytest.mark.asyncio
async def test_memory_saver_thread_restore() -> None:
    from app.agent_runtime.persistence.checkpointer_factory import (
        build_inmemory_checkpointer,
    )

    cp = build_inmemory_checkpointer()
    service = GraphRuntimeService.build_default(checkpointer=cp)
    state = make_empty_state(task_id="t-1", graph_run_id="r-1")
    config = {"configurable": {"thread_id": "t-1"}}

    result_first = await service.ainvoke(state, config=config)
    assert result_first["current_node"] == "initialize_stub"

    snapshot = await service.get_state(
        graph_name="test_plan_generation",
        version="v1",
        config=config,
    )
    assert snapshot.get("current_node") == "initialize_stub"
    assert "initialize_stub" in snapshot.get("completed_nodes", [])