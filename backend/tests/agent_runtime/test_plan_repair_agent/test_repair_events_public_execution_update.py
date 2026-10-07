"""Phase 2.4 test #16: Events + PublicExecutionUpdate — 6 个 REPAIR_* 事件载荷 sanitized。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair
from app.agent_runtime.repair.event_emitter import RepairEventEmitter
from app.agent_runtime.repair.schemas import RepairResult, RepairDecision, ReviewIssue

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    call_regen_decision,
    call_review_decision,
    finish_decision,
    regen_envelope_ok,
    review_envelope_passed,
)


@pytest.mark.asyncio
async def test_repair_emits_sanitized_events(
    fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink,
):
    """REPAIR_* 事件 payload 不含 raw LLM text 或内部 reasoning。"""
    fake_llm.extend([
        call_regen_decision(["sec-A"], ["iss-1"], summary="修"),
        call_review_decision(),
        finish_decision(["iss-1"]),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-A"])],
        "ResultReviewTool": [review_envelope_passed()],
    }

    await run_repair(base_state, llm_client=fake_llm,
                     tool_adapter=stub_adapter, ctx=runtime_ctx)

    event_types = [e["event_type"] for e in in_memory_sink.events]
    # 至少 STARTED + COMPLETED 必出
    assert "repair_started" in event_types
    assert "repair_completed" in event_types
    # 不出现 raw LLM text
    for ev in in_memory_sink.events:
        assert "raw_text" not in (ev.get("payload") or {})
        assert "decision_summary_full" not in (ev.get("payload") or {})


def test_repair_event_emitter_construction(runtime_ctx):
    """RepairEventEmitter 构造不抛,导出 6 个 emit 方法。"""
    emitter = RepairEventEmitter(runtime_ctx)
    assert hasattr(emitter, "emit_repair_started")
    assert hasattr(emitter, "emit_repair_completed")
    assert hasattr(emitter, "emit_repair_fallback")
    assert hasattr(emitter, "emit_repair_budget_exhausted")