"""Phase 2.4 test #11: Repeated strategy — 同 (issue, strategy) 调多次 → fail-fast。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    call_regen_decision,
    fail_decision,
    regen_envelope_ok,
)


@pytest.mark.asyncio
async def test_repair_repeated_strategy(
    fake_llm, stub_adapter, runtime_ctx, base_state,
):
    """LLM 反复用同 (iss-1, regenerate_section) 调 → 第 3 次被 guard 拦 → fallback。"""
    fake_llm.extend([
        call_regen_decision(["sec-A"], ["iss-1"], summary="重复 1"),
        call_regen_decision(["sec-A"], ["iss-1"], summary="重复 2"),
        call_regen_decision(["sec-A"], ["iss-1"], summary="重复 3"),
        fail_decision("permanent_permission_denied"),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-A"])] * 5,
    }

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert result.review_passed is False
    assert result.fallback_reason is not None
    # 不应该调满 3 次 Regen,因为第 3 次会被 guard 拦截;自动复审另计工具调用。
    regen_calls = [
        c for c in stub_adapter.calls
        if c["tool_name"] == "TestPlanRegenTool"
    ]
    assert len(regen_calls) < 3
