"""Phase 2.4 test #14: Checkpoint resume — repair_steps 完整保留;locked_section_ids 持久。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    call_regen_decision,
    regen_envelope_ok,
    review_envelope_passed,
)


@pytest.mark.asyncio
async def test_repair_checkpoint_resume(
    fake_llm, stub_adapter, runtime_ctx, base_state,
):
    """complete 跑完后,repair_steps 列表保留所有步骤审计 + locked_section_ids 不丢。"""
    base_state["locked_section_ids"] = ["sec-LOCKED"]
    fake_llm.extend([
        call_regen_decision(["sec-A"], ["iss-1"], summary="step-1"),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-A"])],
        "ResultReviewTool": [review_envelope_passed()],
    }

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert result.review_passed is True
    # 步骤审计:1 次 LLM regen 决策 + 1 次代码强制自动复审。
    audit = getattr(result, "_audit_steps", None)
    assert audit is not None
    assert len(audit) == 2
    assert audit[1]["auto_re_review"] is True
    # locked_section_ids 不应被覆盖
    assert base_state["locked_section_ids"] == ["sec-LOCKED"]
