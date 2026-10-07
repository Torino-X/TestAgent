"""Repair Agent ToolPermissionGuard (Phase 2.4 — ADR-2.4-7).

Whitelist = ResultReviewTool / TestPlanRegenTool / KnowledgeSearchTool
(not WordExportTool / TestPlanGeneratorTool / DocxFormatCheckTool /
SectionSuggestionTool — these are banned per ADR-2.4-13).

Reuses shared ``ToolPermissionGuard`` from ``app.agent_runtime._shared.permission``,
only differing in whitelist content.
"""

from __future__ import annotations

from typing import FrozenSet

from app.agent_runtime._shared.permission import (  # noqa: F401
    ToolPermissionDenied,
    PermanentPermissionDenied,
    ToolPermissionGuard,
)


REPAIR_TOOL_WHITELIST: FrozenSet[str] = frozenset({
    "ResultReviewTool",
    "TestPlanRegenTool",
    "KnowledgeSearchTool",
    # Phase 2.5+ reserved:
    # "SectionRewriteTool",
    # "TableRepairTool",
    # "SchemaRepairTool",
})


__all__ = [
    "REPAIR_TOOL_WHITELIST",
    "ToolPermissionDenied",
    "PermanentPermissionDenied",
    "ToolPermissionGuard",
]


# module-level note (auto-appended):
# ToolPermissionGuard + REPAIR_TOOL_WHITELIST。
# 关键约束: 3 个工具白名单(result review / regen / kb-search)。
