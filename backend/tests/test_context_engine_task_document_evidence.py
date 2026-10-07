from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.context_engine.models.context import ContextItem, ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind, ContextTrust, SourceType
from app.context_engine.models.context import ContextPlan
from app.context_engine.planning.planner import ContextPlanner
from app.context_engine.runtime.context_engine import _apply_source_contracts
from app.context_engine.selection.quota import SourceQuotaEnforcer, SourceQuotaPolicy
from app.context_engine.selection.selector import ContextSelector
from app.context_engine.sources.task_document_evidence import TaskDocumentEvidenceSourceAdapter
from app.agent_runtime.context.test_plan_evidence import build_test_plan_repair_state_ref
from app.agent_runtime.preparation.agent_loop import _prep_state_ref
from app.context_engine.profiles.registry import TEST_PLAN_PREPARATION_DECIDE_PROFILE


@pytest.mark.asyncio
async def test_task_document_evidence_source_collects_budgeted_parsed_requirement_and_template():
    adapter = TaskDocumentEvidenceSourceAdapter()
    request = ContextRequest(
        user_id="usr_1",
        task_id="task_1",
        conversation_id="conv_1",
        call_site="test_plan.generate.outline",
        state_ref={
            "context_evidence": {
                "parsed_documents": [
                    {
                        "source_ref": "file_requirement",
                        "title": "requirement.docx",
                        "content": "REQ-001 payment must be idempotent",
                    }
                ],
                "template_sections": [
                    {
                        "source_ref": "file_template",
                        "title": "1 Overview",
                        "content": "section_id=overview; field=overview; mode=ai",
                    }
                ],
            }
        },
    )

    result = await adapter.collect(
        request,
        SectionPlan(
            kind=ContextKind.EVIDENCE,
            required=True,
            budget_tokens=1000,
            source_types=["parsed_document", "template_section"],
        ),
        ContextScope(user_id="usr_1", task_id="task_1", conversation_id="conv_1"),
        runtime_context=SimpleNamespace(),
    )

    assert result.degraded is False
    assert [(item.source_type, item.content) for item in result.items] == [
        (SourceType.PARSED_DOCUMENT, "REQ-001 payment must be idempotent"),
        (SourceType.TEMPLATE_SECTION, "section_id=overview; field=overview; mode=ai"),
    ]


@pytest.mark.asyncio
async def test_preparation_state_ref_supplies_required_requirement_and_template_evidence():
    """Preparation CE must receive its parsed task evidence, not an empty ref.

    ``test_plan.preparation.decide`` declares Evidence as required.  The
    dynamic preparation loop therefore has to project the already parsed
    requirement and template state into the same task-local Evidence contract
    used by the generation path.
    """
    state_ref = _prep_state_ref(
        {
            "task_goal": "prepare a test plan",
            "task_type": "test_plan_generation",
            "requirement_summary": "Payment cancellation must be idempotent.",
            "requirement_analysis": {
                "source_ref": "file_requirement",
                "document_name": "requirement.docx",
                "document_title": "Payment requirement",
                "document_structure": [{"title": "Cancellation flow", "level": 1}],
                "table_summaries": [{"title": "Error codes", "rows": 4}],
            },
            "template_structure": {
                "template_name": "QA plan template",
                "sections": [{"title": "1 Overview", "section_id": "overview"}],
                "generation_config": {
                    "ai_fields": [
                        {"field": "overview", "title": "1 Overview", "section_id": "overview"}
                    ]
                },
            },
        }
    )

    evidence = state_ref["context_evidence"]
    assert evidence["parsed_documents"][0]["source_ref"] == "file_requirement"
    assert "Payment cancellation" in evidence["parsed_documents"][0]["content"]
    assert evidence["template_sections"][0]["source_ref"] == "template_structure"

    profile_sources = set(TEST_PLAN_PREPARATION_DECIDE_PROFILE.required_sections[-1].source_types)
    assert {"parsed_document", "template_section"}.issubset(profile_sources)

    adapter = TaskDocumentEvidenceSourceAdapter()
    result = await adapter.collect(
        ContextRequest(
            user_id="usr_1",
            task_id="task_1",
            call_site="test_plan.preparation.decide",
            state_ref=state_ref,
        ),
        SectionPlan(
            kind=ContextKind.EVIDENCE,
            required=True,
            budget_tokens=15_000,
            source_types=list(profile_sources),
        ),
        ContextScope(user_id="usr_1", task_id="task_1"),
        runtime_context=SimpleNamespace(),
    )

    assert result.failure_code is None
    assert {item.source_type for item in result.items} == {
        SourceType.PARSED_DOCUMENT,
        SourceType.TEMPLATE_SECTION,
    }


def test_preparation_profile_keeps_its_bounded_decision_input_as_current_goal():
    """A preparation decision must reach the model even when its input is >1K tokens.

    The historical Preparation prompt deliberately combines the user's request,
    requirement summary, template summary, and prior decision state.  Its bounded
    input is then passed through CE as CURRENT_GOAL.  Treating that authoritative
    request as a 1K-token optional fragment caused the selector to drop the only
    current-goal item and abort before the LLM call with required_unmet.
    """
    plan = ContextPlanner().plan(
        ContextRequest(
            user_id="usr_1",
            task_id="task_1",
            conversation_id="conv_1",
            call_site="test_plan.preparation.decide",
            current_user_message="bounded preparation decision input",
        ),
        model_context_window=200_000,
    )

    def item(kind: ContextKind, source_type: SourceType, token_count: int) -> ContextItem:
        return ContextItem(
            item_id=f"{kind.value}:{source_type.value}",
            kind=kind,
            source_type=source_type,
            source_ref="test",
            content=f"{kind.value} content",
            authority=100,
            priority=100,
            estimated_tokens=token_count,
            trust=ContextTrust.TRUSTED_INSTRUCTION,
        )

    selected = ContextSelector().select(
        plan,
        {
            ContextKind.SYSTEM_RULES.value: [item(ContextKind.SYSTEM_RULES, SourceType.SYSTEM, 10)],
            ContextKind.CALL_CONTRACT.value: [item(ContextKind.CALL_CONTRACT, SourceType.SYSTEM, 10)],
            # Mirrors the 3,828-character real preparation payload from
            # task_03060fc8: it is bounded upstream but can exceed 1K tokens.
            ContextKind.CURRENT_GOAL.value: [item(ContextKind.CURRENT_GOAL, SourceType.CONVERSATION, 1_001)],
            ContextKind.TASK_STATE.value: [item(ContextKind.TASK_STATE, SourceType.TASK_STATE, 10)],
            ContextKind.EVIDENCE.value: [item(ContextKind.EVIDENCE, SourceType.PARSED_DOCUMENT, 10)],
        },
    )

    assert any(item.kind is ContextKind.CURRENT_GOAL for item in selected.included)


@pytest.mark.asyncio
async def test_task_document_evidence_collects_repair_generated_content_and_review_result():
    """Repair profiles must receive the real generated plan and review findings.

    This is the production failure contract from task_6ad21701: repair requires
    generated_content/review_result Evidence before an artifact exists.
    """
    adapter = TaskDocumentEvidenceSourceAdapter()
    request = ContextRequest(
        user_id="usr_1",
        task_id="task_1",
        call_site="test_plan.repair.plan",
        state_ref={
            "context_evidence": {
                "generated_content": [
                    {
                        "source_ref": "task_1:generated:scope",
                        "title": "3 测试范围",
                        "content": "覆盖预约、签到与取消预约。",
                    }
                ],
                "review_results": [
                    {
                        "source_ref": "task_1:review",
                        "title": "ResultReviewTool",
                        "content": "scope 必须返回数组。",
                    }
                ],
            }
        },
    )

    result = await adapter.collect(
        request,
        SectionPlan(
            kind=ContextKind.EVIDENCE,
            required=True,
            budget_tokens=1000,
            source_types=["generated_content", "review_result"],
        ),
        ContextScope(user_id="usr_1", task_id="task_1"),
        runtime_context=SimpleNamespace(),
    )

    assert [item.source_type.value for item in result.items] == [
        "generated_content",
        "review_result",
    ]


@pytest.mark.asyncio
async def test_repair_state_ref_normalizes_direct_generated_sections_and_review_issues():
    """The v3 state shape must satisfy the repair profile's required Evidence."""
    state_ref = build_test_plan_repair_state_ref(
        test_plan_content={
            "generated_sections": [
                {
                    "section_id": "body_18_level_1",
                    "title": "overview",
                    "content": {"body": "project overview"},
                }
            ],
            "review_issues": [
                {
                    "issue_id": "issue-1",
                    "section_id": "body_18_level_1",
                    "message": "placeholder remains",
                }
            ],
        }
    )
    adapter = TaskDocumentEvidenceSourceAdapter()
    result = await adapter.collect(
        ContextRequest(
            user_id="usr_1",
            task_id="task_1",
            call_site="test_plan.repair.plan",
            state_ref=state_ref,
        ),
        SectionPlan(
            kind=ContextKind.EVIDENCE,
            required=True,
            budget_tokens=1000,
            source_types=["generated_content", "review_result"],
        ),
        ContextScope(user_id="usr_1", task_id="task_1"),
        runtime_context=SimpleNamespace(),
    )

    assert [item.source_type.value for item in result.items] == [
        "generated_content",
        "review_result",
    ]


@pytest.mark.asyncio
async def test_repair_state_ref_normalizes_pre_package_graph_sections():
    state_ref = build_test_plan_repair_state_ref(
        test_plan_content={
            "sections": [
                {"section_id": "body_28_level_1", "content": "strategy"}
            ],
            "schema_issues": [
                {"section_id": "body_28_level_1", "message": "placeholder"}
            ],
        }
    )
    adapter = TaskDocumentEvidenceSourceAdapter()
    result = await adapter.collect(
        ContextRequest(
            user_id="usr_1",
            task_id="task_1",
            call_site="test_plan.repair.plan",
            state_ref=state_ref,
        ),
        SectionPlan(
            kind=ContextKind.EVIDENCE,
            required=True,
            budget_tokens=1000,
            source_types=["generated_content", "review_result"],
        ),
        ContextScope(user_id="usr_1", task_id="task_1"),
        runtime_context=SimpleNamespace(),
    )

    assert [item.source_type.value for item in result.items] == [
        "generated_content",
        "review_result",
    ]


@pytest.mark.asyncio
async def test_task_document_evidence_rejects_unprepared_oversized_source_instead_of_tail_truncation():
    adapter = TaskDocumentEvidenceSourceAdapter()
    request = ContextRequest(
        user_id="usr_1",
        task_id="task_1",
        call_site="test_plan.generate.outline",
        state_ref={
            "context_evidence": {
                "parsed_documents": [{"content": "需求内容" * 1000}],
                "template_sections": [{"content": "模板章节" * 1000}],
            }
        },
    )
    section = SectionPlan(
        kind=ContextKind.EVIDENCE,
        required=True,
        budget_tokens=100,
        source_types=[SourceType.PARSED_DOCUMENT, SourceType.TEMPLATE_SECTION],
    )
    result = await adapter.collect(
        request,
        section,
        ContextScope(user_id="usr_1", task_id="task_1"),
        runtime_context=SimpleNamespace(),
    )

    assert result.items == []
    assert result.degraded is True
    assert result.failure_code == "context.source.requirement_evidence_unprepared"


@pytest.mark.asyncio
async def test_task_document_evidence_keeps_every_prepared_chunk_and_its_source_span():
    adapter = TaskDocumentEvidenceSourceAdapter()
    request = ContextRequest(
        user_id="usr_1",
        task_id="task_1",
        call_site="test_plan.generate.outline",
        state_ref={
            "context_evidence": {
                "coverage_manifest": {"coverage_complete": True, "chunk_count": 2},
                "parsed_documents": [
                    {
                        "source_ref": "file_requirement#chunk-1",
                        "title": "requirement.docx [1/2]",
                        "content": "REQ-HEAD extracted evidence",
                        "chunk_id": "chunk-1",
                        "start_char": 0,
                        "end_char": 100,
                        "source_sha256": "a" * 64,
                        "locked": True,
                    },
                    {
                        "source_ref": "file_requirement#chunk-2",
                        "title": "requirement.docx [2/2]",
                        "content": "REQ-TAIL extracted evidence",
                        "chunk_id": "chunk-2",
                        "start_char": 100,
                        "end_char": 200,
                        "source_sha256": "b" * 64,
                        "locked": True,
                    },
                ],
                "template_sections": [],
            }
        },
    )
    result = await adapter.collect(
        request,
        SectionPlan(
            kind=ContextKind.EVIDENCE,
            required=True,
            budget_tokens=1,
            source_types=[SourceType.PARSED_DOCUMENT],
        ),
        ContextScope(user_id="usr_1", task_id="task_1"),
        runtime_context=SimpleNamespace(),
    )

    assert [item.content for item in result.items] == [
        "REQ-HEAD extracted evidence",
        "REQ-TAIL extracted evidence",
    ]
    assert all(item.metadata["locked"] is True for item in result.items)
    assert [item.metadata["chunk_id"] for item in result.items] == ["chunk-1", "chunk-2"]
    assert result.degraded is False


@pytest.mark.asyncio
async def test_task_document_evidence_survives_source_contracts_after_preparation():
    adapter = TaskDocumentEvidenceSourceAdapter()
    request = ContextRequest(
        user_id="usr_1",
        task_id="task_1",
        call_site="test_plan.generate.outline",
        state_ref={
            "context_evidence": {
                "parsed_documents": [{"content": "prepared requirement", "locked": True}],
                "template_sections": [{"content": "prepared template", "locked": True}],
            }
        },
    )
    section = SectionPlan(
        kind=ContextKind.EVIDENCE,
        required=True,
        budget_tokens=100,
        source_types=[SourceType.PARSED_DOCUMENT, SourceType.TEMPLATE_SECTION],
    )
    result = await adapter.collect(
        request,
        section,
        ContextScope(user_id="usr_1", task_id="task_1"),
        runtime_context=SimpleNamespace(),
    )

    plan = ContextPlan(
        profile_key="test",
        profile_version="v1",
        model_context_window=1000,
        input_budget=500,
        output_reserve=100,
        runtime_reserve=100,
        safety_margin=10,
        section_plans={"evidence": section},
    )
    constrained, dropped = _apply_source_contracts(
        plan,
        {"evidence": result.items},
        quota_enforcer=SourceQuotaEnforcer(SourceQuotaPolicy()),
        locked_sections=[],
    )

    assert [item.source_type for item in constrained["evidence"]] == [
        SourceType.PARSED_DOCUMENT,
        SourceType.TEMPLATE_SECTION,
    ]
    assert dropped == []
