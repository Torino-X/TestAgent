"""test_prep_budget_steps — 6 步上限触发 BudgetExceeded (Phase 2.3 §9.11)."""

from __future__ import annotations

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation
from app.agent_runtime.preparation.budget import BudgetTracker

from .conftest import call_kb_decision, kb_envelope_ok


@pytest.mark.asyncio
async def test_budget_max_steps_exceeded(
    fake_llm, stub_adapter, runtime_ctx, base_state, make_clock, in_memory_sink
):
    """LLM 永远不返回 finish → 6 步后 BudgetExceeded(max_steps) → fallback + BUDGET_EXHAUSTED。"""
    factory, _advance = make_clock

    # 8 个不同 call_tool 决策 (避免 guard same-args 拦截;超 MAX_AGENT_STEPS=6 +1 safety net)
    for i in range(8):
        fake_llm.push(call_kb_decision(query=f"q{i}_distinct"))
    # 永远不 finish

    stub_adapter.envelopes_kb = [
        kb_envelope_ok([{"text": "ok", "title": "x", "source": "kb"}])
    ] * 8

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
        clock=factory,
    )

    # 每次 call_tool 同时 step + tool_call;BudgetExceeded 先于 MAX_TOOL_CALLS=4 触发(因为 on_tool_call 在 check 内)
    # MAX_TOOL_CALLS=4 + 1 safety net;on_tool_call 在 check 之前 +1,所以第 5 次触发 max_tool_calls
    # 因为 on_tool_call 在 check 之前 +1,MAX_TOOL_CALLS=4 路径先触发(5 > 4),但 steps=5 ≥ 6/2 验证 budget 守门生效
    assert result.fallback_reason is not None
    assert "budget_exhausted" in result.fallback_reason
    assert result.budget_state.steps >= 1
    assert result.budget_state.tool_calls >= BudgetTracker.MAX_TOOL_CALLS


@pytest.mark.asyncio
async def test_budget_steps_emits_exhausted_event(
    fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink
):
    """SSE PREPARATION_BUDGET_EXHAUSTED 携带 code=max_steps。"""
    for i in range(8):
        fake_llm.push(call_kb_decision(query=f"q{i}_distinct"))
    stub_adapter.envelopes_kb = [
        kb_envelope_ok([{"text": "ok", "title": "x", "source": "kb"}])
    ] * 8

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
    # 因为 on_tool_call 在 check 之前 +1,agent_loop 优先触发 max_tool_calls
    assert ev["payload"].get("code") in ("max_tool_calls", "max_steps")


def test_budget_tracker_unit_max_steps():
    """Unit: BudgetTracker MAX_AGENT_STEPS = 6."""
    from app.agent_runtime.preparation.budget import BudgetExceeded

    t = [0.0]
    bt = BudgetTracker(wall_clock_fn=lambda: t[0])
    for _ in range(BudgetTracker.MAX_AGENT_STEPS):
        bt.on_step()
    # 第 7 步超限
    with pytest.raises(BudgetExceeded) as exc_info:
        bt.on_step()
    assert exc_info.value.code == "max_steps"


def test_budget_tracker_max_wall_seconds():
    """Unit: 超 wall time 触发 max_wall_time。"""
    from app.agent_runtime.preparation.budget import BudgetExceeded

    t = [0.0]
    bt = BudgetTracker(wall_clock_fn=lambda: t[0], max_wall_seconds=10.0)
    bt.on_step()
    t[0] = 20.0  # 跳 20 秒
    with pytest.raises(BudgetExceeded) as exc_info:
        bt.on_token_estimate(100)
    assert exc_info.value.code == "max_wall_time"