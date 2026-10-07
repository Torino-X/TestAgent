"""Incremental Agent ToolPermissionGuard (Phase 2.5).

Phase 2.5 工具白名单:

* ``TestPlanRegenTool`` — 重写指定 section_ids(主修复工具)
* ``ResultReviewTool`` — 增量后的局部重审
* ``KnowledgeSearchTool`` — 补充证据(可选)
* ``WordExportTool`` — 增量后导出新 artifact(版本化)
* ``DocxFormatCheckTool`` — 增量后格式检查

白名单比 Repair 多 WordExportTool + DocxFormatCheckTool —— 因为 Incremental
流程必须把"修改后的章节 + 新 export 落库成 version_no=N+1 artifact"。
``TestPlanGeneratorTool`` 仍 banned(整份重生不属于 Incremental)。

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


INCREMENTAL_TOOL_WHITELIST: FrozenSet[str] = frozenset({
    "TestPlanRegenTool",
    "ResultReviewTool",
    "KnowledgeSearchTool",
    "WordExportTool",
    "DocxFormatCheckTool",
    # Phase 2.6+ reserved (精细化工具):
    # "SectionRewriteTool",
    # "TableRepairTool",
    # "SchemaRepairTool",
})


__all__ = [
    "INCREMENTAL_TOOL_WHITELIST",
    "ToolPermissionDenied",
    "PermanentPermissionDenied",
    "ToolPermissionGuard",
]

# module-level note (auto-appended):
# ToolPermissionGuard(增量子图)。
# 关键约束: 与 prep/repair 同 _shared。
