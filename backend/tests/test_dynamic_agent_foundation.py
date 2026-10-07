from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.atomic_capability_registry import (
    AtomicCapabilityRegistry,
    AtomicSideEffect,
)
from app.agent_runtime.dynamic_agent.schemas import (
    DynamicObservation,
    DynamicPlan,
    DynamicPlanStep,
    DynamicStepStatus,
)


def test_atomic_registry_default_exposes_only_safe_dynamic_v1_capabilities() -> None:
    registry = AtomicCapabilityRegistry.default()

    visible_keys = {spec.capability_key for spec in registry.planner_visible()}

    assert {
        "word_document_parse",
        "knowledge_search",
        "evidence_analysis",
    }.issubset(visible_keys)
    assert "TemplateParserTool" not in visible_keys
    assert "TestPlanGeneratorTool" not in visible_keys
    assert "WordExportTool" not in visible_keys

    for spec in registry.planner_visible():
        assert spec.side_effect in {AtomicSideEffect.READ, AtomicSideEffect.EXTERNAL_READ}
        assert spec.risk_level in {"low", "medium"}
        assert spec.input_schema
        assert spec.output_schema
        assert spec.backend_target


def test_atomic_registry_rejects_unknown_visible_capability() -> None:
    registry = AtomicCapabilityRegistry.default()

    assert registry.get("missing_capability") is None
    with pytest.raises(ValueError, match="unknown_atomic_capability"):
        registry.require("missing_capability")


def test_dynamic_plan_schema_requires_structured_steps() -> None:
    step = DynamicPlanStep(
        step_id="step_1",
        title="解析Word文档",
        action_type="tool",
        capability_key="word_document_parse",
        input_refs=["attachment:0"],
        depends_on=[],
        success_criteria=["document_text_non_empty"],
    )
    plan = DynamicPlan(
        goal="分析当前上传的 Word 文档",
        revision=1,
        steps=[step],
    )

    assert plan.steps[0].status == DynamicStepStatus.PENDING
    assert plan.revision == 1

    with pytest.raises(ValidationError):
        DynamicPlan(goal="bad", revision=0, steps=[])


def test_dynamic_observation_is_runtime_data_not_narrative() -> None:
    observation = DynamicObservation(
        observation_id="obs_1",
        step_id="step_1",
        capability_key="word_document_parse",
        status="success",
        summary="已解析 17 个章节",
        result_ref="state:tool_results.step_1",
        facts={"section_count": 17},
    )

    dumped = observation.model_dump()

    assert dumped["facts"]["section_count"] == 17
    assert "narrative" not in dumped
