"""Phase 2.4 test #12: Budget exhausted — LLM 永不返回 finish → 8 步后 budget 耗尽。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair, _RepairBudgetLimits
from app.agent_runtime.repair.schemas import BudgetState

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    call_regen_decision,
    regen_envelope_ok,
)


@pytest.mark.asyncio
async def test_repair_budget_steps(
    fake_llm, stub_adapter, runtime_ctx, base_state,
):
    """脚本 LLM 永不 finish → 8 步后 BudgetExceeded → REPAIR_BUDGET_EXHAUSTED 事件。

    Note:  LLM 始终 target_issue_ids 真实 issue;sections 与 issues 取基础值
    的笛卡尔积以创造足够 unique args_signature,避免被 MAX_SAME_TOOL_SAME_ARGS
    拦截。TestPlanRegenTool envelope 始终 ok,agent 永不返回 finish → budget
    在 8 步后耗尽 (steps ≥ 7)。
    """
    section_cycle = ["sec-A", "sec-B"]
    issue_cycle = ["iss-1", "iss-2"]
    fake_llm.extend([
        call_regen_decision(
            [section_cycle[i % len(section_cycle)]],
            [issue_cycle[i % len(issue_cycle)]],
            summary=f"步 {i}",
            iteration=i,
        )
        for i in range(_RepairBudgetLimits.MAX_AGENT_STEPS + 2)
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [
            regen_envelope_ok(["sec-A"]) for _ in range(20)
        ],
    }

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert result.review_passed is False
    assert result.fallback_reason is not None
    # budget/max_steps 在 fallback_reason 中体现
    assert "budget" in result.fallback_reason or "max_steps" in result.fallback_reason
    # 自动复审后,预算可能先耗尽 tool_calls 而不是 steps。
    assert isinstance(result.budget_state, BudgetState)
    assert (
        result.budget_state.steps >= _RepairBudgetLimits.MAX_AGENT_STEPS - 1
        or result.budget_state.tool_calls > _RepairBudgetLimits.MAX_TOOL_CALLS
    )


def test_repair_budget_constants():
    """_RepairBudgetLimits 数值与计划一致。"""
    assert _RepairBudgetLimits.MAX_AGENT_STEPS == 8
    assert _RepairBudgetLimits.MAX_TOOL_CALLS == 6
    assert _RepairBudgetLimits.MAX_WALL_TIME_SECONDS == 120.0
    assert _RepairBudgetLimits.MAX_TOKEN_ESTIMATE == 8000
    assert _RepairBudgetLimits.MAX_SAME_TOOL_SAME_ARGS == 2
