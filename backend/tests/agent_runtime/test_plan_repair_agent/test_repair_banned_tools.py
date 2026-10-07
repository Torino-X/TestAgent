"""Phase 2.4 test #8: Banned tools — WordExportTool 等永远被拦截。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.decision_filter import filter_repair_decision
from app.agent_runtime.repair.permission import REPAIR_TOOL_WHITELIST, ToolPermissionGuard
from app.agent_runtime.repair.schemas import RepairDecision
from app.agent_runtime.repair.scope_guard import BANNED_TOOLS_IN_REPAIR


def test_repair_tool_whitelist_minimal():
    """REPAIR_TOOL_WHITELIST 仅含三个工具。"""
    assert REPAIR_TOOL_WHITELIST == frozenset({
        "ResultReviewTool", "TestPlanRegenTool", "KnowledgeSearchTool",
    })


def test_banned_tools_in_repair_comprehensive():
    """BANNED_TOOLS_IN_REPAIR 必须含 WordExportTool 等。"""
    assert "WordExportTool" in BANNED_TOOLS_IN_REPAIR
    assert "TestPlanGeneratorTool" in BANNED_TOOLS_IN_REPAIR
    assert "DocxFormatCheckTool" in BANNED_TOOLS_IN_REPAIR


def test_filter_blocks_word_export_tool():
    """filter_repair_decision 拒绝 WordExportTool 调用。"""
    decision = RepairDecision(
        action="call_tool", tool_name="WordExportTool",
        tool_arguments={}, decision_summary="违规调用",
    )
    guard = ToolPermissionGuard()
    clean, blocked = filter_repair_decision(decision, guard=guard,
                                            review_issues=[], locked_section_ids=[])
    assert clean.action == "fail"
    assert any("WordExportTool" in str(b) for b in blocked)


def test_filter_blocks_after_two_violations():
    """连续 2 次白名单外工具 → PermanentPermissionDenied。"""
    from app.agent_runtime.repair.permission import PermanentPermissionDenied

    decision = RepairDecision(
        action="call_tool", tool_name="WordExportTool",
        tool_arguments={}, decision_summary="违规",
    )
    guard = ToolPermissionGuard(fail_fast_after=2)
    # 第一次拒绝,第二次触发永久拒绝
    filter_repair_decision(decision, guard=guard, review_issues=[], locked_section_ids=[])
    with pytest.raises(PermanentPermissionDenied):
        filter_repair_decision(decision, guard=guard, review_issues=[], locked_section_ids=[])