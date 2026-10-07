"""Tests for RepairAgent decision filtering."""

from __future__ import annotations

from app.agent_runtime.repair.decision_filter import filter_repair_decision
from app.agent_runtime.repair.permission import (
    REPAIR_TOOL_WHITELIST,
    ToolPermissionGuard,
)
from app.agent_runtime.repair.schemas import RepairDecision, ReviewIssue
from app.agent_runtime.repair.agent_loop import _format_unresolved_repair_summary


def _guard() -> ToolPermissionGuard:
    return ToolPermissionGuard(whitelist=REPAIR_TOOL_WHITELIST)


def _backfill_issue() -> ReviewIssue:
    return ReviewIssue(
        issue_id="no_tables_for_body_18_level_1:body_18_level_1:0",
        rule_id="no_tables_for_body_18_level_1",
        kind="template_backfill",
        severity="block",
        section_id="body_18_level_1",
        message="模板未声明该章节的表格占位，但 LLM 返回了 tables 数据（1 张表）",
        evidence="tables data",
        expected_rule="section_matches_template",
        suggested_strategy="regenerate_section",
    )


def test_filter_accepts_rule_id_alias_and_backfills_regen_issues():
    issue = _backfill_issue()
    decision = RepairDecision(
        action="call_tool",
        tool_name="TestPlanRegenTool",
        target_issue_ids=["no_tables_for_body_18_level_1"],
        target_section_ids=["body_18_level_1"],
        suggested_strategy="regenerate_section",
        decision_summary="重写 body_18_level_1，移除非法表格数据",
        tool_arguments={
            "section_ids": ["body_18_level_1"],
            "issues": [],
            "test_plan_content": {},
            "template_structure": {},
            "generation_config_subset": {},
        },
    )

    clean, blocked = filter_repair_decision(
        decision,
        guard=_guard(),
        review_issues=[issue],
        locked_section_ids=[],
    )

    assert blocked == []
    assert clean.action == "call_tool"
    assert clean.tool_name == "TestPlanRegenTool"
    assert clean.target_issue_ids == [issue.issue_id]
    assert clean.tool_arguments["section_ids"] == ["body_18_level_1"]
    assert clean.tool_arguments["issues"][0]["issue_id"] == issue.issue_id


def test_filter_still_blocks_unreviewed_section():
    issue = _backfill_issue()
    decision = RepairDecision(
        action="call_tool",
        tool_name="TestPlanRegenTool",
        target_issue_ids=["no_tables_for_body_18_level_1"],
        target_section_ids=["body_99_level_1"],
        suggested_strategy="regenerate_section",
        decision_summary="尝试越界重写 body_99_level_1",
        tool_arguments={
            "section_ids": ["body_99_level_1"],
            "issues": [],
            "test_plan_content": {},
            "template_structure": {},
            "generation_config_subset": {},
        },
    )

    clean, blocked = filter_repair_decision(
        decision,
        guard=_guard(),
        review_issues=[issue],
        locked_section_ids=[],
    )

    assert clean.action == "fail"
    assert blocked[0]["reason"] == "scope_guard_violation"


def test_unresolved_summary_does_not_claim_export_will_continue():
    summary = _format_unresolved_repair_summary(
        fallback_reason="decision_filter_fail:scope_guard_violation",
        remaining_issue_summary="**body_18_level_1**: 模板未声明表格占位",
    )

    assert "继续导出" not in summary
    assert "决策未通过范围校验" in summary


def test_filter_allows_bulk_scope_only_for_json_truncated_recovery():
    issues = [
        ReviewIssue(
            issue_id=f"iss-{idx}",
            rule_id="generator_schema_missing_field",
            kind="generator_missing_field",
            severity="block",
            section_id=f"sec-{idx}",
            message=f"sec-{idx} missing after truncation",
            evidence='{"source_error":"json_truncated"}',
            suggested_strategy="regenerate_section",
        )
        for idx in range(5)
    ]
    decision = RepairDecision(
        action="call_tool",
        tool_name="TestPlanRegenTool",
        target_issue_ids=[issue.issue_id for issue in issues],
        target_section_ids=[issue.section_id for issue in issues if issue.section_id],
        suggested_strategy="bulk_regenerate_missing_sections",
        decision_summary="批量恢复截断缺失章节",
        tool_arguments={
            "section_ids": [issue.section_id for issue in issues if issue.section_id],
            "issues": [],
            "test_plan_content": {},
            "template_structure": {},
            "generation_config_subset": {},
            "recovery_mode": "json_truncated",
            "bulk_repair": True,
        },
    )

    clean, blocked = filter_repair_decision(
        decision,
        guard=_guard(),
        review_issues=issues,
        locked_section_ids=[],
    )

    assert blocked == []
    assert clean.action == "call_tool"
    assert clean.tool_arguments["recovery_mode"] == "json_truncated"
    assert clean.tool_arguments["section_ids"] == [
        "sec-0", "sec-1", "sec-2", "sec-3", "sec-4"
    ]


def test_filter_still_blocks_large_non_truncation_scope():
    issues = [
        ReviewIssue(
            issue_id=f"iss-{idx}",
            rule_id="rule",
            kind="missing",
            severity="block",
            section_id=f"sec-{idx}",
            message=f"sec-{idx} missing",
            evidence="missing",
            suggested_strategy="regenerate_section",
        )
        for idx in range(5)
    ]
    decision = RepairDecision(
        action="call_tool",
        tool_name="TestPlanRegenTool",
        target_issue_ids=[issue.issue_id for issue in issues],
        target_section_ids=[issue.section_id for issue in issues if issue.section_id],
        suggested_strategy="regenerate_section",
        decision_summary="普通问题不允许一次修太多章节",
        tool_arguments={
            "section_ids": [issue.section_id for issue in issues if issue.section_id],
            "issues": [],
            "test_plan_content": {},
            "template_structure": {},
            "generation_config_subset": {},
        },
    )

    clean, blocked = filter_repair_decision(
        decision,
        guard=_guard(),
        review_issues=issues,
        locked_section_ids=[],
    )

    assert clean.action == "fail"
    assert blocked[0]["reason"] == "scope_guard_violation"
