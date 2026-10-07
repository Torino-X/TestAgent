from __future__ import annotations

from dataclasses import replace

import pytest

from app.agent_runtime.repair.agent_loop import (
    _repair_context_manifest,
    _task_state_ref,
    run_repair,
)
from app.context_engine.errors import (
    ContextEngineError,
    ContextEngineFailure,
    ContextEngineStage,
)


def test_repair_context_state_ref_preserves_direct_v3_generation_and_review_state():
    state_ref = _task_state_ref(
        {
            "task_goal": "repair the failed test plan",
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

    evidence = state_ref["context_evidence"]
    assert len(evidence["generated_content"]) == 1
    assert len(evidence["review_results"]) == 1
    assert _repair_context_manifest(state_ref) == {
        "generated_content": 1,
        "review_results": 1,
        "task_state_keys": 1,
    }


@pytest.mark.asyncio
async def test_repair_ce_selection_failure_keeps_safe_input_diagnostics(
    monkeypatch,
    fake_llm,
    stub_adapter,
    runtime_ctx,
    base_state,
):
    from app.context_engine import feature_flags

    class SelectionFailingBridge:
        available = True

        async def generate(self, **_kwargs):
            raise ContextEngineFailure(
                ContextEngineError(
                    code="context.selection.required_unmet",
                    detail="required section unavailable",
                    stage=ContextEngineStage.SELECTION,
                    safe_metadata={"section_id": "evidence"},
                )
            )

    monkeypatch.setattr(
        feature_flags,
        "require_agent_context_migration",
        lambda *_args, **_kwargs: None,
    )
    ctx = replace(runtime_ctx, context_llm_invoker=SelectionFailingBridge())

    result = await run_repair(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=ctx,
    )

    assert result.fallback_reason == "context_selection_required_unmet:evidence"
    assert base_state["repair_context_diagnostic"] == {
        "code": "context.selection.required_unmet",
        "stage": "selection",
        "required_section": "evidence",
        "input_manifest": {
            "generated_content": 3,
            "review_results": 1,
            "task_state_keys": 0,
        },
    }


@pytest.mark.asyncio
async def test_repair_uses_ce_with_graph_sections_and_reaches_regeneration(
    monkeypatch,
    fake_llm,
    stub_adapter,
    runtime_ctx,
    base_state,
):
    from app.context_engine import feature_flags

    decision = {
        "action": "call_tool",
        "tool_name": "TestPlanRegenTool",
        "tool_arguments": {
            "section_ids": ["sec-A"],
            "issues": [{"issue_id": "iss-1", "section_id": "sec-A"}],
            "generation_config_subset": {},
        },
        "target_issue_ids": ["iss-1"],
        "target_section_ids": ["sec-A"],
        "suggested_strategy": "regenerate_section",
        "decision_summary": "repair the failed section",
        "public_update": "repairing",
        "expected_result": "review passes",
        "confidence": 0.8,
    }

    class SuccessfulBridge:
        available = True
        calls: list[dict] = []

        async def generate(self, **kwargs):
            self.calls.append(kwargs)

            class BridgeResult:
                def as_profile_result(self):
                    return type(
                        "ProfileResult",
                        (),
                        {"success": True, "parsed": decision, "error_message": None},
                    )()

            return BridgeResult()

    monkeypatch.setattr(
        feature_flags,
        "require_agent_context_migration",
        lambda *_args, **_kwargs: None,
    )
    bridge = SuccessfulBridge()
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [
            {
                "success": True,
                "tool_name": "TestPlanRegenTool",
                "data": {
                    "section_ids": ["sec-A"],
                    "sections": [{"section_id": "sec-A", "content": "rewritten"}],
                },
                "error": None,
            }
        ],
        "ResultReviewTool": [
            {
                "success": True,
                "tool_name": "ResultReviewTool",
                "data": {
                    "level": "passed",
                    "review_issues": [],
                    "block_issues": [],
                    "issues": [],
                },
                "error": None,
            }
        ],
    }
    ctx = replace(runtime_ctx, context_llm_invoker=bridge, task_flag_resolver=object())

    result = await run_repair(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=ctx,
    )

    assert result.review_passed is True
    assert [call["tool_name"] for call in stub_adapter.calls] == [
        "TestPlanRegenTool",
        "ResultReviewTool",
    ]
    state_ref = bridge.calls[0]["task_state_ref"]
    assert len(state_ref["context_evidence"]["generated_content"]) == 3
    assert len(state_ref["context_evidence"]["review_results"]) == 1
    assert "ResultReviewTool" in bridge.calls[0]["system_prompt"]
