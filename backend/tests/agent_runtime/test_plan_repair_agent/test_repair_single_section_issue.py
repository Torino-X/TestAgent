"""Phase 2.4 test #1: Single section issue — TestPlanRegenTool 只调一次,1 个 section。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair
from app.agent_runtime.repair.schemas import ReviewIssue
from app.agent_runtime.repair.issue_parser import parse_review_issues

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    call_regen_decision,
    call_review_decision,
    finish_decision,
    regen_envelope_ok,
    review_envelope_passed,
)


@pytest.mark.asyncio
async def test_repair_single_section_issue(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """1 个 section 的 block issue → 1 次 regen + 1 次 review → passed。"""
    fake_llm.extend([
        call_regen_decision(["sec-A"], ["iss-1"], summary="修复 sec-A"),
        call_review_decision(summary="复审 sec-A"),
        finish_decision(["iss-1"], summary="修复完成"),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-A"])],
        "ResultReviewTool": [review_envelope_passed()],
    }

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert result.review_passed is True
    assert "iss-1" in result.issues_resolved
    assert result.issues_remaining == []
    assert result.modified_section_ids == ["sec-A"]
    assert result.fallback_reason is None
    assert result.tool_calls_used == 2  # regen + review


@pytest.mark.asyncio
async def test_parse_review_issues_filters_severity(base_state):
    """parse_review_issues 只保留 severity=block + repairable=True。"""
    issues = parse_review_issues(base_state["review_result"], locked_section_ids=[])
    assert len(issues) == 2
    assert all(i.severity == "block" for i in issues)
    assert all(isinstance(i, ReviewIssue) for i in issues)