"""test_prep_budget_tool_calls — 4 次 tool_call 上限触发 BudgetExceeded (Phase 2.3 §9.12)."""

from __future__ import annotations

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation
from app.agent_runtime.preparation.budget import BudgetTracker

from .conftest import call_kb_decision, finish_decision, kb_envelope_ok


@pytest.mark.asyncio
async def test_budget_max_tool_calls_exceeded(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """4 次 tool_call 后,BudgetExceeded(max_tool_calls) 触发。"""
    # 5 次 call_kb:前 4 次调工具,第 5 次 on_step + 后续 on_tool_call 超限
    for i in range(5):
        fake_llm.push(call_kb_decision(query=f"q{i}"))
    # 兜底 finish (实际不会跑到,因为 budget 先 raise)
    fake_llm.push(finish_decision(summary="unused"))

    stub_adapter.envelopes_kb = [
        kb_envelope_ok([{"text": f"r{i}", "title": "x", "source": "kb"}])
        for i in range(5)
    ]

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # on_tool_call 在 check 之前 +1;第 5 次 on_tool_call 触发 raise,snapshot.tool_calls = 5
    assert result.fallback_reason is not None
    assert "budget_exhausted" in result.fallback_reason
    assert "max_tool_calls" in result.fallback_reason
    assert result.budget_state.tool_calls == BudgetTracker.MAX_TOOL_CALLS + 1


@pytest.mark.asyncio
async def test_tool_call_budget_emits_exhausted(
    fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink
):
    """tool_call 超限触发 PREPARATION_BUDGET_EXHAUSTED 事件 + code=max_tool_calls。"""
    for i in range(5):
        fake_llm.push(call_kb_decision(query=f"q{i}"))
    stub_adapter.envelopes_kb = [
        kb_envelope_ok([{"text": "r", "title": "x", "source": "kb"}])
    ] * 5

    await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    import asyncio
    await asyncio.sleep(0.05)
    events = in_memory_sink.collect()
    types = [e["event_type"] for e in events]
    assert "preparation_budget_exhausted" in types

    ev = next(e for e in events if e["event_type"] == "preparation_budget_exhausted")
    assert ev["payload"]["code"] == "max_tool_calls"


@pytest.mark.asyncio
async def test_normal_finish_under_tool_call_budget(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """3 次 KB 后 finish — 远低于 4 次上限,无 budget exhaust。"""
    for i in range(3):
        fake_llm.push(call_kb_decision(query=f"q{i}"))
    fake_llm.push(finish_decision(summary="信息已充足"))

    stub_adapter.envelopes_kb = [
        kb_envelope_ok([{"text": "r", "title": "x", "source": "kb"}])
    ] * 3

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.fallback_reason is None
    assert result.budget_state.tool_calls == 3
    assert result.budget_state.steps == 4  # 3 calls + 1 finish step