"""Phase 2.4 test #17: Never enter export until passed — route_after_review 严格守卫。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    call_regen_decision,
    call_review_decision,
    finish_decision,
    review_envelope_failed,
    review_envelope_passed,
    regen_envelope_ok,
    fail_decision,
)


@pytest.mark.asyncio
async def test_repair_blocks_export_until_passed(
    fake_llm, stub_adapter, runtime_ctx, base_state,
):
    """review_passed=False 时,run_repair 永不声称完成;必须由主图决定是否走 export。"""
    # 注入循环:regen → review(仍 failed) → fail
    fake_llm.extend([
        call_regen_decision(["sec-A"], ["iss-1"], summary="尝试"),
        call_review_decision(),
        fail_decision("无法解决"),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-A"])],
        "ResultReviewTool": [review_envelope_failed(["iss-1"], ["sec-A"])],
    }

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert result.review_passed is False
    # fallback_reason 必填
    assert result.fallback_reason is not None
    # 永不返工 read 出口
    assert result.modified_section_ids == ["sec-A"]


@pytest.mark.asyncio
async def test_repair_passed_unblocks_export_decision(
    fake_llm, stub_adapter, runtime_ctx, base_state,
):
    """review_passed=True 时,主图可放心走 export。"""
    fake_llm.extend([
        call_regen_decision(["sec-A", "sec-B"], ["iss-1", "iss-2"]),
        call_review_decision(),
        finish_decision(["iss-1", "iss-2"]),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-A", "sec-B"])],
        "ResultReviewTool": [review_envelope_passed()],
    }

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert result.review_passed is True
    assert result.fallback_reason is None