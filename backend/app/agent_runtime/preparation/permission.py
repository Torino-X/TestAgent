"""Tool permission guard re-export (Phase 2.4 — ADR-2.4-1).

The implementation moved to ``app.agent_runtime._shared.permission``.
``PREPARATION_TOOL_WHITELIST`` 仍是 preparation 私有的常量(只 1 个工具)。
"""

from __future__ import annotations

from typing import FrozenSet

from app.agent_runtime._shared.permission import (
    PermanentPermissionDenied,
    ToolPermissionDenied,
    ToolPermissionGuard,
)


# ── 白名单 (Phase 2.3 单一工具,与 9 工具主白名单分离) ────────────────

PREPARATION_TOOL_WHITELIST: FrozenSet[str] = frozenset({
    "KnowledgeSearchTool",
})


__all__ = [
    "PREPARATION_TOOL_WHITELIST",
    "ToolPermissionDenied",
    "PermanentPermissionDenied",
    "ToolPermissionGuard",
]

# module-level note (auto-appended):
# ToolPermissionGuard + PREPARATION_TOOL_WHITELIST。
# 白名单 + 同 args 重复 guard,reject 转 make_to_fail_decision。
# 关键约束: 不白名单的工具直接 reject,不允许 silent fallback。
