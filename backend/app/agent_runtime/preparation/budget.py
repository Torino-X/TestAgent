"""BudgetTracker re-export (Phase 2.4 — ADR-2.4-1).

The implementation moved to ``app.agent_runtime._shared.budget``.
This module keeps the import path stable for Phase 2.3 tests.
"""

from app.agent_runtime._shared.budget import BudgetExceeded, BudgetState, BudgetTracker

__all__ = ["BudgetTracker", "BudgetExceeded", "BudgetState"]

# module-level note (auto-appended):
# BudgetTracker + BudgetExceeded(prep 子图专用)。
# 限制: MAX_AGENT_STEPS=6 / MAX_TOOL_CALLS=4 / WALL=120s / TOKEN=6000 / MAX_SAME_ARGS=2。
# 关键约束: 超限 raise BudgetExceeded(soft-fail, 上层 catch 当 retry 边界)。
