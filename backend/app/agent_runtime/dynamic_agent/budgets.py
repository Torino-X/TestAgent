"""Dynamic Agent loop budgets."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DynamicAgentBudgets:
    max_plan_steps: int = 8
    max_replans: int = 2
    max_tool_calls: int = 10
    max_same_capability_repeats: int = 2
    max_llm_analysis_steps: int = 4



# module-level note (auto-appended):
# DynamicAgentBudgets — 动态 Agent 步 / token 上限。
# 关键约束: 与 _shared BudgetTracker 兼容。
