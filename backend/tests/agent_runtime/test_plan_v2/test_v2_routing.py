"""Phase 2.1 v2 — 4 路由测试。"""

from __future__ import annotations

import pytest

from app.agent_runtime.graphs.test_plan.versions.v2.routing import (
    route_after_format_check,
    route_after_review,
)


# ── 9. route_after_review failed → regenerate ─────────────────────────────


def test_route_after_review_failed_loops_back_to_regenerate():
    """review_result.level=failed + block_issues + review_loop_count < MAX → regenerate。"""
    state = {
        "review_result": {
            "level": "failed",
            "block_issues": [
                {"section_id": "1", "issue_type": "missing_assertion"},
            ],
        },
        "review_loop_count": 1,
    }
    assert route_after_review(state) == "regenerate_sections"


# ── 10. route_after_review passed → prepare_export ────────────────────────


def test_route_after_review_passes_proceeds_to_prepare():
    """review_result.level=passed → prepare_export,不进 regen。"""
    state = {
        "review_result": {"level": "passed", "block_issues": []},
        "review_loop_count": 1,
    }
    assert route_after_review(state) == "prepare_export"


def test_route_after_review_warning_routes_to_prepare():
    """level=warning 也算通过(无 block_issues),直接 prepare_export。"""
    state = {
        "review_result": {
            "level": "warning",
            "block_issues": [],
        },
        "review_loop_count": 2,
    }
    assert route_after_review(state) == "prepare_export"


def test_route_after_review_budget_exhausted_routes_to_prepare():
    """failed 但 review_loop_count 已达 MAX → prepare_export,避免无限循环。"""
    state = {
        "review_result": {
            "level": "failed",
            "block_issues": [{"section_id": "1", "issue_type": "missing_assertion"}],
        },
        "review_loop_count": 99,
    }
    assert route_after_review(state) == "prepare_export"


# ── 11. route_after_format_check loss_detected → pause ───────────────────


def test_route_after_format_check_loss_detected_routes_to_pause():
    """level=loss_detected + format_loop_count ≤ MAX → pause_for_legacy_format_decision。"""
    state = {
        "format_check_result": {
            "level": "loss_detected",
            "losses": [{"component": "table", "severity": "high"}],
        },
        "format_loop_count": 2,
    }
    assert (
        route_after_format_check(state) == "pause_for_legacy_format_decision"
    )


def test_route_after_format_check_passed_routes_to_finalize():
    """level=passed → finalize_task。"""
    state = {
        "format_check_result": {"level": "passed"},
        "format_loop_count": 1,
    }
    assert route_after_format_check(state) == "finalize_task"


# ── 12. route_after_format_check failed retry within budget ──────────────


def test_route_after_format_check_failed_retry_within_budget():
    """level=failed + format_loop_count < MAX → prepare_export(更高保真度重试)。"""
    state = {
        "format_check_result": {
            "level": "failed",
            "error": "schema_check_failed",
        },
        "format_loop_count": 1,
    }
    assert route_after_format_check(state) == "prepare_export"


def test_route_after_format_check_failed_budget_exhausted_routes_to_pause():
    """level=failed + format_loop_count ≥ MAX → pause_for_legacy_format_decision(让用户决策)。"""
    state = {
        "format_check_result": {
            "level": "failed",
            "error": "schema_check_failed",
        },
        "format_loop_count": 99,
    }
    assert (
        route_after_format_check(state) == "pause_for_legacy_format_decision"
    )