"""Tests for v3 review-result routing."""

from __future__ import annotations

from app.agent_runtime.graphs.test_plan.versions.v3.routing import (
    NODE_FAIL_TASK,
    NODE_PREPARE_EXPORT,
    NODE_REPAIR_SUBGRAPH,
    route_after_review,
)


def test_failed_review_with_standard_block_issue_routes_to_repair():
    state = {
        "review_result": {
            "level": "failed",
            "review_issues": [
                {
                    "rule_id": "empty_ai_section",
                    "kind": "empty_section",
                    "severity": "block",
                    "section_id": "strategy",
                    "message": "用户选择 AI 生成的章节为空",
                }
            ],
            "block_issues": [],
        },
        "repair_agent_enabled": True,
        "repair_loop_count": 0,
    }

    assert route_after_review(state) == NODE_REPAIR_SUBGRAPH


def test_empty_standard_review_issues_do_not_fallback_to_legacy_blocks():
    state = {
        "review_result": {
            "level": "passed",
            "review_issues": [],
            "block_issues": [
                {
                    "rule_id": "stale_legacy",
                    "section_id": "strategy",
                    "message": "上一轮遗留问题",
                }
            ],
        },
        "repair_agent_enabled": True,
        "repair_loop_count": 0,
    }

    assert route_after_review(state) == NODE_PREPARE_EXPORT


def test_exhausted_review_recovery_with_blocking_issues_fails_instead_of_exporting():
    state = {
        "review_result": {
            "level": "failed",
            "review_issues": [
                {"severity": "block", "section_id": "strategy", "message": "still invalid"}
            ],
        },
        "repair_agent_enabled": True,
        "repair_loop_count": 999,
        "review_loop_count": 999,
    }

    assert route_after_review(state) == NODE_FAIL_TASK
