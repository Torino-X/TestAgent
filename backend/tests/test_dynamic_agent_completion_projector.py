from __future__ import annotations

from app.agent_runtime.dynamic_agent.completion_projector import (
    DynamicAgentCompletionProjector,
)


def test_completion_projector_is_idempotent_per_task() -> None:
    projector = DynamicAgentCompletionProjector()
    state = {
        "task_public_id": "task_dyn_004",
        "final_answer": "最终答案",
    }

    first = projector.project(state)
    second = projector.project(state)

    assert first is not None
    assert first["task_public_id"] == "task_dyn_004"
    assert first["role"] == "assistant"
    assert first["content"] == "最终答案"
    assert second is None


def test_completion_projector_refuses_empty_final_answer() -> None:
    projector = DynamicAgentCompletionProjector()

    assert projector.project({"task_public_id": "task_dyn_005", "final_answer": ""}) is None
