"""Phase 2.4 test #4: Table issues (kind=table_header_whitelist)。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    _make_review_result,
    call_regen_decision,
    call_review_decision,
    finish_decision,
    regen_envelope_ok,
    review_envelope_passed,
)


@pytest.mark.asyncio
async def test_repair_table_issues(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """table header violation → regen 修。"""
    base_state["review_result"] = _make_review_result(
        ["iss-1"], ["sec-A"], level="failed", kind="table_header_whitelist"
    )
    fake_llm.extend([
        call_regen_decision(["sec-A"], ["iss-1"], summary="修正表头"),
        call_review_decision(),
        finish_decision(["iss-1"]),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-A"])],
        "ResultReviewTool": [review_envelope_passed()],
    }

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert result.review_passed is True
    assert "sec-A" in result.modified_section_ids