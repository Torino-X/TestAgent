from __future__ import annotations

import pytest

from app.agent_runtime.graph_registry import GraphRegistry
from app.agent_runtime.graph_runtime_service import GraphRuntimeService
from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator


@pytest.mark.asyncio
async def test_run_incremental_passes_runtime_context_from_config(monkeypatch):
    """Worker/Dispatcher 进入 Coordinator 后,RuntimeContext 不应在子图入口丢失。"""
    from app.agent_runtime.incremental import subgraph as subgraph_mod

    runtime_ctx = object()
    factory_calls = {"count": 0}
    captured = {}

    def context_factory(_state):
        factory_calls["count"] += 1
        return runtime_ctx

    async def fake_run_incremental_subgraph(
        state, *, ctx, config=None, checkpointer=None,
    ):
        captured["ctx"] = ctx
        captured["config_ctx"] = (
            (config or {}).get("configurable") or {}
        ).get("runtime_context")
        return {
            **state,
            "task_status": "completed",
            "current_node": "incremental_finish",
            "incremental_result": {"success": True},
        }

    monkeypatch.setattr(
        subgraph_mod,
        "run_incremental_subgraph",
        fake_run_incremental_subgraph,
    )

    registry = GraphRegistry.build_default_v2_v3()
    coordinator = LangGraphRunCoordinator(
        registry=registry,
        runtime=GraphRuntimeService(registry),
        checkpointer=None,
        context_factory=context_factory,
    )

    outcome = await coordinator.run_incremental(
        task_id="task_inc_ctx",
        graph_run_id="run-task_inc_ctx",
        initial_payload={
            "incremental_intent": {"restored": True},
            "source_artifact_public_id": "art_ctx",
            "modification_idempotency_key": "idem-ctx",
        },
    )

    assert outcome.completed is True
    assert captured["ctx"] is runtime_ctx
    assert captured["config_ctx"] is runtime_ctx
    assert factory_calls["count"] == 1


@pytest.mark.asyncio
async def test_run_incremental_preserves_source_artifact_context_in_graph_state(monkeypatch):
    """Persisted source-plan fields must reach the incremental subgraph."""
    from app.agent_runtime.incremental import subgraph as subgraph_mod

    captured = {}

    async def fake_run_incremental_subgraph(
        state, *, ctx, config=None, checkpointer=None,
    ):
        captured["state"] = state
        return {
            **state,
            "task_status": "completed",
            "current_node": "incremental_finish",
            "incremental_result": {"success": True},
        }

    monkeypatch.setattr(
        subgraph_mod,
        "run_incremental_subgraph",
        fake_run_incremental_subgraph,
    )

    registry = GraphRegistry.build_default_v2_v3()
    coordinator = LangGraphRunCoordinator(
        registry=registry,
        runtime=GraphRuntimeService(registry),
        checkpointer=None,
        context_factory=lambda _state: object(),
    )
    source_context = {
        "test_plan_content": {"section_package": {"generated_sections": []}},
        "template_structure": {"generation_config": {"ai_fields": []}},
        "review_result": {"level": "failed", "review_issues": []},
        "review_standard": {"required": True},
    }

    await coordinator.run_incremental(
        task_id="task_inc_state",
        graph_run_id="run-task_inc_state",
        initial_payload={
            "incremental_intent": {"restored": True},
            "source_artifact_public_id": "art_state",
            "modification_idempotency_key": "idem-state",
            **source_context,
        },
    )

    assert captured["state"]["test_plan_content"] == source_context["test_plan_content"]
    assert captured["state"]["template_structure"] == source_context["template_structure"]
    assert captured["state"]["review_result"] == source_context["review_result"]
    assert captured["state"]["review_standard"] == source_context["review_standard"]
