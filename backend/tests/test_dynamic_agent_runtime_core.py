from __future__ import annotations

import pytest

from app.agent.atomic_capability_registry import AtomicCapabilityRegistry
from app.agent_runtime.dynamic_agent.budgets import DynamicAgentBudgets
from app.agent_runtime.dynamic_agent.executor import DynamicStepExecutor
from app.agent_runtime.dynamic_agent.plan_validator import DynamicPlanValidator
from app.agent_runtime.dynamic_agent.planner import DynamicPlanner
from app.agent_runtime.dynamic_agent.replanner import DynamicReplanner
from app.agent_runtime.dynamic_agent.synthesizer import DynamicSynthesizer
from app.agent_runtime.dynamic_agent.verifier import DynamicVerifier, VerificationDecision
from app.agent_runtime.dynamic_agent.schemas import DynamicPlan, DynamicPlanStep


def _doc_state() -> dict:
    return {
        "goal": "分析当前上传的 Word 文档",
        "target_capability": "document_qa",
        "operation": "analyze",
        "attachment_refs": [{"ref": "attachment:0", "file_public_id": "file_doc", "file_ext": ".docx"}],
    }


def test_planner_creates_structured_document_analysis_plan() -> None:
    plan = DynamicPlanner(AtomicCapabilityRegistry.default()).plan(_doc_state())

    assert isinstance(plan, DynamicPlan)
    assert plan.plan_id.startswith("dyn_plan_")
    assert [step.capability_key for step in plan.steps] == [
        "word_document_parse",
        "evidence_analysis",
    ]
    assert plan.revision == 1
    assert plan.steps[0].input_refs == ["attachment:0"]


def test_plan_validator_rejects_unknown_capability_fail_closed() -> None:
    plan = DynamicPlan(
        goal="bad",
        revision=1,
        steps=[
            DynamicPlanStep(
                step_id="step_1",
                title="bad",
                action_type="tool",
                capability_key="unknown_tool",
                success_criteria=["never_run"],
            )
        ],
    )

    result = DynamicPlanValidator(AtomicCapabilityRegistry.default()).validate(plan, _doc_state())

    assert result.valid is False
    assert "unknown_capability:unknown_tool" in result.errors


@pytest.mark.asyncio
async def test_executor_turns_capability_result_into_observation() -> None:
    async def _handler(step, state):
        return {
            "success": True,
            "summary": "parsed",
            "data": {"text_content": "正文", "document_structure": {"sections": []}},
        }

    step = DynamicPlanStep(
        step_id="step_1",
        title="解析Word文档",
        action_type="tool",
        capability_key="word_document_parse",
        input_refs=["attachment:0"],
        success_criteria=["document_text_non_empty", "document_structure_non_empty"],
    )

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        handlers={"word_document_parse": _handler},
    ).execute(step, _doc_state())

    assert observation.status == "success"
    assert observation.step_id == "step_1"
    assert observation.facts["text_length"] == 2
    assert observation.facts["has_structure"] is True


def test_verifier_marks_completed_plan_complete() -> None:
    state = _doc_state()
    state["plan"] = DynamicPlanner(AtomicCapabilityRegistry.default()).plan(state).model_dump(mode="python")
    for step in state["plan"]["steps"]:
        step["status"] = "completed"
    state["observations"] = [
        {"step_id": "step_1", "status": "success"},
        {"step_id": "step_2", "status": "success"},
    ]

    result = DynamicVerifier().verify(state)

    assert result.decision == VerificationDecision.COMPLETE
    assert result.gaps == []


def test_verifier_requests_user_when_clarification_is_pending() -> None:
    state = _doc_state()
    state["awaiting_user"] = True
    state["clarification"] = {
        "question": "Which uploaded document should be analyzed?",
        "required_input": "attachment_selection",
    }
    state["plan"] = DynamicPlanner(AtomicCapabilityRegistry.default()).plan(state).model_dump(mode="python")

    result = DynamicVerifier().verify(state)

    assert result.decision == VerificationDecision.NEED_USER
    assert result.gaps == ["user_input_required"]
    assert "Which uploaded document" in result.user_safe_reason


def test_replanner_increments_revision_and_preserves_completed_steps() -> None:
    plan = DynamicPlan(
        goal="分析文档",
        revision=1,
        steps=[
            DynamicPlanStep(
                step_id="step_1",
                title="已完成",
                action_type="tool",
                capability_key="word_document_parse",
                success_criteria=["ok"],
                status="completed",
            ),
            DynamicPlanStep(
                step_id="step_2",
                title="失败",
                action_type="analysis",
                capability_key="evidence_analysis",
                success_criteria=["ok"],
                status="failed",
            ),
        ],
    )

    next_plan = DynamicReplanner().replan(plan, gaps=["analysis_missing"])

    assert next_plan.revision == 2
    assert next_plan.steps[0].status == "completed"
    assert next_plan.steps[1].status == "superseded"
    assert next_plan.steps[-1].step_id == "step_3"


def test_budget_rejects_excessive_plan() -> None:
    plan = DynamicPlan(
        goal="too many",
        revision=1,
        steps=[
            DynamicPlanStep(
                step_id=f"step_{idx}",
                title=f"step {idx}",
                action_type="analysis",
                capability_key="evidence_analysis",
                success_criteria=["ok"],
            )
            for idx in range(1, 10)
        ],
    )

    result = DynamicPlanValidator(
        AtomicCapabilityRegistry.default(),
        budgets=DynamicAgentBudgets(max_plan_steps=8),
    ).validate(plan, _doc_state())

    assert result.valid is False
    assert "budget_exceeded:max_plan_steps" in result.errors


def test_synthesizer_uses_verified_evidence_not_raw_tool_output() -> None:
    answer = DynamicSynthesizer().synthesize(
        {
            "goal": "总结文档",
            "analysis_results": {"step_2": "文档描述了预约签到流程。"},
            "observations": [{"summary": "已解析文档"}],
        }
    )

    assert "预约签到流程" in answer
    assert "raw" not in answer.lower()
