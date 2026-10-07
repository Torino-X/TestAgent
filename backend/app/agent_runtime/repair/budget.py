"""Repair Agent budget — re-export from shared (ADR-2.4-1)."""

from app.agent_runtime._shared.budget import (  # noqa: F401
    BudgetExceeded,
    BudgetTracker,
)


__all__ = ["BudgetTracker", "BudgetExceeded"]


# module-level note (auto-appended):
# BudgetTracker + BudgetExceeded(repair 子图专用)。
# 关键约束: 与 prep 共享 _shared.BudgetTracker,本模块只占位。
