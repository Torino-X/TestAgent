from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.agent_runtime.graph_registry import GraphRegistry
from app.agent_runtime.graph_runtime_service import GraphRuntimeService
from app.agent_runtime.graphs.dynamic_agent import (
    GRAPH_NAME_DYNAMIC_AGENT,
    GRAPH_VERSION_DYNAMIC_AGENT_V1,
)
from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator


def test_coordinator_initial_state_preserves_dynamic_agent_graph_name() -> None:
    registry = GraphRegistry.build_default_v2_v3()
    coordinator = LangGraphRunCoordinator(
        registry=registry,
        runtime=GraphRuntimeService(registry),
        checkpointer=None,
    )

    state = coordinator._initial_state(
        task_id="task_dyn_003",
        graph_run_id="run-task_dyn_003",
        payload={
            "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
            "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
            "goal": "analyze known evidence",
            "target_capability": "general_chat",
            "operation": "analyze",
        },
    )

    assert state["graph_name"] == GRAPH_NAME_DYNAMIC_AGENT
    assert state["graph_version"] == GRAPH_VERSION_DYNAMIC_AGENT_V1
    assert state["goal"] == "analyze known evidence"
    assert state["target_capability"] == "general_chat"


class _FakeCompiledGraph:
    def __init__(self) -> None:
        self.aget_state = AsyncMock(
            return_value=type(
                "Snapshot",
                (),
                {
                    "values": {
                        "task_id": "task_dyn_resume",
                        "graph_run_id": "run-task_dyn_resume",
                        "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
                        "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
                        "task_status": "running",
                    }
                },
            )()
        )
        self.ainvoke = AsyncMock(
            return_value={
                "task_id": "task_dyn_resume",
                "graph_run_id": "run-task_dyn_resume",
                "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
                "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
                "task_status": "completed",
            }
        )


@pytest.mark.asyncio
async def test_resume_thread_uses_restored_dynamic_agent_graph_name() -> None:
    registry = GraphRegistry(name="dynamic-only-test")
    compiled = _FakeCompiledGraph()
    registry.register(
        GRAPH_NAME_DYNAMIC_AGENT,
        GRAPH_VERSION_DYNAMIC_AGENT_V1,
        compiled,
        schema_version=1,
    )
    coordinator = LangGraphRunCoordinator(
        registry=registry,
        runtime=GraphRuntimeService(registry),
        checkpointer=None,
    )
    coordinator._load_checkpoint_state_by_thread_id = AsyncMock(
        return_value={
            "task_id": "task_dyn_resume",
            "graph_run_id": "run-task_dyn_resume",
            "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
            "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
        }
    )

    outcome = await coordinator.resume_thread(
        task_id="task_dyn_resume",
        graph_run_id="run-task_dyn_resume",
    )

    assert outcome.completed is True
    assert outcome.final_state["graph_name"] == GRAPH_NAME_DYNAMIC_AGENT
    compiled.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_thread_restores_completed_dynamic_agent_from_checkpointer() -> None:
    from app.agent_runtime.persistence.checkpointer_factory import (
        build_inmemory_checkpointer,
    )

    checkpointer = build_inmemory_checkpointer()
    registry = GraphRegistry.build_default_v2_v3(checkpointer=checkpointer)
    coordinator = LangGraphRunCoordinator(
        registry=registry,
        runtime=GraphRuntimeService(registry),
        checkpointer=checkpointer,
    )

    first = await coordinator.run_pre_confirm(
        task_id="task_dyn_checkpoint",
        graph_run_id="run-task_dyn_checkpoint",
        initial_payload={
            "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
            "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
            "goal": "Summarize the available evidence",
            "target_capability": "general_chat",
            "operation": "analyze",
        },
    )

    resumed = await coordinator.resume_thread(
        task_id="task_dyn_checkpoint",
        graph_run_id="run-task_dyn_checkpoint",
    )

    assert first.completed is True
    assert resumed.completed is True
    assert resumed.final_state["graph_name"] == GRAPH_NAME_DYNAMIC_AGENT
    assert resumed.final_state["graph_version"] == GRAPH_VERSION_DYNAMIC_AGENT_V1
    assert resumed.final_state["task_status"] == "completed"


@pytest.mark.asyncio
async def test_dynamic_agent_need_user_maps_to_paused_outcome() -> None:
    registry = GraphRegistry.build_default_v2_v3()
    coordinator = LangGraphRunCoordinator(
        registry=registry,
        runtime=GraphRuntimeService(registry),
        checkpointer=None,
    )

    outcome = await coordinator.run_pre_confirm(
        task_id="task_dyn_need_user_outcome",
        graph_run_id="run-task_dyn_need_user_outcome",
        initial_payload={
            "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
            "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
            "goal": "Choose the source document",
            "target_capability": "general_chat",
            "operation": "analyze",
            "awaiting_user": True,
            "clarification": {
                "question": "Which document should I analyze?",
                "required_input": "attachment_selection",
            },
            "plan": {
                "goal": "Choose the source document",
                "revision": 1,
                "status": "active",
                "steps": [
                    {
                        "step_id": "step_1",
                        "title": "Wait for user selection",
                        "action_type": "analysis",
                        "capability_key": "evidence_analysis",
                        "input_refs": [],
                        "depends_on": [],
                        "success_criteria": ["user_selection_received"],
                        "status": "completed",
                    }
                ],
            },
            "observations": [{"step_id": "step_1", "status": "success"}],
        },
    )

    assert outcome.completed is False
    assert outcome.paused is True
    assert outcome.pause_marker == "dynamic_agent_need_user"
    assert outcome.task_status == "waiting_user"
