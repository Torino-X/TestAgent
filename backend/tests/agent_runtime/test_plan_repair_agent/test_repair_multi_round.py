"""Phase 2.4 test #10: Multi-round repair — 第 1 轮修 s1,复审发现 s2 仍 block;第 2 轮修 s2。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    _make_review_result,
    call_regen_decision,
    regen_envelope_ok,
    review_envelope_failed,
    review_envelope_passed,
)


@pytest.mark.asyncio
async def test_repair_multi_round(
    fake_llm, stub_adapter, runtime_ctx, base_state,
):
    """第 1 轮只修 sec-A;复审发现 sec-B 仍有 issue;第 2 轮修 sec-B;复审通过。"""
    base_state["review_result"] = _make_review_result(
        ["iss-1", "iss-2"], ["sec-A", "sec-B"], level="failed"
    )

    fake_llm.extend([
        # Round 1: 只修 sec-A
        call_regen_decision(["sec-A"], ["iss-1"], summary="先修 sec-A"),
        # Round 2: 修 sec-B
        call_regen_decision(["sec-B"], ["iss-2"], summary="再修 sec-B"),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [
            regen_envelope_ok(["sec-A"]),
            regen_envelope_ok(["sec-B"]),
        ],
        "ResultReviewTool": [
            review_envelope_failed(["iss-2"], ["sec-B"]),
            review_envelope_passed(),
        ],
    }

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert result.review_passed is True
    assert set(result.modified_section_ids) == {"sec-A", "sec-B"}
    assert result.tool_calls_used == 4  # regen + review + regen + review
    assert result.rounds_used >= 2
