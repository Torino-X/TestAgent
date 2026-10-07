"""Phase 2.4 test #13: Dynamic failure fallback — LLM 调失败 → emit REPAIR_FALLBACK → fallback node 调 regen。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair
from app.agent_runtime.repair.fallback import run_legacy_review_repair_fallback

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    call_regen_decision,
    tool_envelope_failure,
)


@pytest.mark.asyncio
async def test_repair_dynamic_failure_fallback(
    fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink,
):
    """LLM 调工具 → envelope.success=False → run_repair 走 fallback。"""
    fake_llm.extend([
        call_regen_decision(["sec-A"], ["iss-1"], summary="尝试修"),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [tool_envelope_failure("TestPlanRegenTool", "REGEN_TIMEOUT")],
    }

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert result.review_passed is False
    assert result.fallback_reason is not None
    event_types = [e["event_type"] for e in in_memory_sink.events]
    assert "repair_fallback" in event_types


@pytest.mark.asyncio
async def test_fallback_delegates_to_regenerate(base_state, runtime_ctx):
    """run_legacy_review_repair_fallback 直接调 regenerate_sections_node。"""
    result = await run_legacy_review_repair_fallback(base_state, ctx=runtime_ctx)
    assert isinstance(result, dict)
    assert result.get("repair_fallback_reason") == "fallback_to_legacy_regen"