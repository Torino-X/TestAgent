"""Incremental Agent budget — re-export from shared (ADR-2.4-1)."""

from app.agent_runtime._shared.budget import (  # noqa: F401
    BudgetExceeded,
    BudgetTracker,
)


__all__ = ["BudgetTracker", "BudgetExceeded"]

# module-level note (auto-appended):
# BudgetTracker + BudgetExceeded(incremental 专用)。
# 关键约束: 与 prep/repair 共享 _shared.BudgetTracker。
