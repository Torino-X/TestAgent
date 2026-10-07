"""Phase 2.4 test #9: Successful repair E2E — emit REPAIR_COMPLETED,review_passed=True。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    call_regen_decision,
    call_review_decision,
    finish_decision,
    regen_envelope_ok,
    review_envelope_passed,
)


@pytest.mark.asyncio
async def test_repair_successful_emits_completed(
    fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink,
):
    """happy path: 2 issues → regen → review → finish。sink 收到 REPAIR_* 事件。"""
    fake_llm.extend([
        call_regen_decision(["sec-A", "sec-B"], ["iss-1", "iss-2"], summary="双修"),
        call_review_decision(summary="复审"),
        finish_decision(["iss-1", "iss-2"], summary="全过"),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-A", "sec-B"])],
        "ResultReviewTool": [review_envelope_passed()],
    }

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert result.review_passed is True
    assert set(result.issues_resolved) == {"iss-1", "iss-2"}
    assert result.fallback_reason is None
    # 事件校验
    event_types = [e["event_type"] for e in in_memory_sink.events]
    assert "repair_started" in event_types
    assert "repair_completed" in event_types
    assert "repair_fallback" not in event_types