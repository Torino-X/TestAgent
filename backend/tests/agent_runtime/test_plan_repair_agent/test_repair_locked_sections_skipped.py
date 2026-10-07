"""Phase 2.4 test #6: User-locked sections — locked_section_ids 中的 issue 永不被修复。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair
from app.agent_runtime.repair.issue_parser import parse_review_issues

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    _make_review_result,
    call_regen_decision,
    call_review_decision,
    finish_decision,
    regen_envelope_ok,
    review_envelope_passed,
)


@pytest.mark.asyncio
async def test_repair_locked_sections_skipped(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """sec-A 被锁,issue_parser + scope_guard 双重过滤 → LLM 只修 sec-B。"""
    base_state["locked_section_ids"] = ["sec-A"]
    base_state["review_result"] = _make_review_result(
        ["iss-1", "iss-2"], ["sec-A", "sec-B"], level="failed"
    )

    issues = parse_review_issues(base_state["review_result"],
                                 locked_section_ids=["sec-A"])
    assert all(i.section_id != "sec-A" for i in issues)
    assert {i.section_id for i in issues} == {"sec-B"}

    fake_llm.extend([
        call_regen_decision(["sec-B"], ["iss-2"], summary="修 sec-B"),
        call_review_decision(),
        finish_decision(["iss-2"]),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-B"])],
        "ResultReviewTool": [review_envelope_passed()],
    }

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert "sec-A" not in result.modified_section_ids
    assert "sec-B" in result.modified_section_ids


def test_parse_review_issues_filters_locked(base_state):
    """issue_parser 在 sec-A 被锁时跳过 iss-1。"""
    base_state["locked_section_ids"] = ["sec-A"]
    issues = parse_review_issues(base_state["review_result"],
                                 locked_section_ids=["sec-A"])
    issue_ids = {i.issue_id for i in issues}
    assert "iss-1" not in issue_ids
    assert "iss-2" in issue_ids