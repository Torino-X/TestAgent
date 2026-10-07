"""Tests for RepairAgent review issue parsing."""

from __future__ import annotations

from app.agent_runtime.repair.issue_parser import parse_review_issues


def test_parse_review_issues_stringifies_structured_evidence():
    issues = parse_review_issues(
        {
            "review_issues": [
                {
                    "rule_id": "backfill_contract",
                    "kind": "backfill_contract",
                    "severity": "block",
                    "section_id": "body_18_level_1",
                    "message": "模板未声明该章节的表格占位，但 JSON 返回了 1 张表格数据",
                    "evidence": {
                        "table_count": 1,
                        "table_rows": [[
                            {"异常列A": "template has no table placeholder"}
                        ]],
                    },
                }
            ]
        }
    )

    assert len(issues) == 1
    assert issues[0].rule_id == "backfill_contract"
    assert isinstance(issues[0].evidence, str)
    assert "table_count" in issues[0].evidence
    assert "异常列A" in issues[0].evidence


def test_empty_standard_review_issues_does_not_fallback_to_legacy_issues():
    issues = parse_review_issues(
        {
            "level": "passed",
            "review_issues": [],
            "issues": [
                {
                    "rule_id": "unknown_rule",
                    "kind": "improvement",
                    "message": "缺少可改进章节",
                }
            ],
            "block_issues": [
                {
                    "rule_id": "old_block",
                    "section_id": "strategy",
                    "message": "旧审查遗留 block",
                }
            ],
        }
    )

    assert issues == []


def test_warning_standard_review_issues_does_not_fallback_to_legacy_issues():
    issues = parse_review_issues(
        {
            "level": "warning",
            "review_issues": [
                {
                    "rule_id": "content_suggestion",
                    "kind": "suggestion",
                    "severity": "warning",
                    "section_id": "strategy",
                    "message": "建议补充风险说明",
                }
            ],
            "issues": [
                {
                    "rule_id": "unknown_rule",
                    "kind": "legacy_issue",
                    "message": "旧兼容字段里的非阻断问题",
                }
            ],
        }
    )

    assert issues == []
