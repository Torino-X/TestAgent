"""Phase 2.9C narrative governance — detail policy + dedup tests."""

from __future__ import annotations

import pytest

from app.agent_runtime._shared.narrative_governance.dedup import (
    decide,
    default_suppress,
)
from app.agent_runtime._shared.narrative_governance.policy import project
from app.agent_runtime._shared.narrative_governance.schemas import (
    NarrativeDetailLevel,
    NarrativeGovernanceContext,
)


def _ctx(level: str = "standard", **kw) -> NarrativeGovernanceContext:
    base = {
        "task_id": "t1",
        "agent_name": "PreparationAgent",
        "update_kind": "agent_decision_update",
        "action": "call_tool",
        "route": "execute_tool",
        "detail_level": level,
    }
    base.update(kw)
    return NarrativeGovernanceContext.model_validate(base)


# ── Detail level projection ─────────────────────────────────────────


def test_concise_drops_empty_details_but_keeps_headline():
    candidate = {
        "headline": "ok", "summary": "long summary here",
        "impact": "i", "next_action": "n", "details": [],
    }
    projected = project(candidate, context=_ctx("concise"))
    assert projected is not None
    assert projected["headline"] == "ok"
    assert projected["details"] == []


def test_concise_suppresses_low_value_event():
    candidate = {
        "headline": "",
        "summary": "",
        "impact": "",
        "next_action": "",
        "details": [],
        "level": "info",
    }
    projected = project(candidate, context=_ctx("concise", action="call_tool"))
    # No impact / next_action / summary + non-critical ⇒ suppress.
    assert projected is None


def test_detailed_truncates_summary_within_budget():
    summary = "x" * 500
    candidate = {"headline": "ok", "summary": summary, "details": []}
    projected = project(candidate, context=_ctx("detailed"))
    assert projected["summary"].endswith("…")
    assert len(projected["summary"]) <= 240


def test_standard_level_keeps_summary_full_within_budget():
    candidate = {"headline": "ok", "summary": "short summary", "details": []}
    projected = project(candidate, context=_ctx("standard"))
    assert projected["summary"] == "short summary"
    assert projected["detail_level"] == "standard"


def test_details_truncated_to_budget_with_overflow_marker():
    details = [f"item {i}" for i in range(10)]
    candidate = {"headline": "ok", "summary": "ok", "details": details}
    projected = project(candidate, context=_ctx("standard"))
    # standard budget = 4 detail items
    assert len(projected["details"]) <= 5  # 4 items + a marker line
    assert any("已省略" in line for line in projected["details"])


def test_concise_critical_event_is_not_suppressed():
    candidate = {
        "headline": "工具失败",
        "summary": "", "impact": "", "next_action": "",
        "details": [], "level": "error",
    }
    projected = project(candidate, context=_ctx("concise", action="fail"))
    assert projected is not None
    assert projected["headline"]


# ── Dedup ──────────────────────────────────────────────────────────


def test_dedup_first_occurrence_is_emit():
    decision = decide(
        context=_ctx(action="call_tool"),
        occurrence=1,
        previous_outcome_code=None,
    )
    assert decision.action == "emit"
    assert decision.aggregate_template == ""


def test_dedup_second_occurrence_is_aggregate_repeat():
    decision = decide(
        context=_ctx(action="call_tool", outcome_code="success"),
        occurrence=2,
        previous_outcome_code="success",
    )
    assert decision.action == "aggregate"
    assert decision.aggregate_template == "repeat"


def test_dedup_third_occurrence_is_aggregate_stable():
    decision = decide(
        context=_ctx(action="call_tool", outcome_code="success"),
        occurrence=3,
        previous_outcome_code="success",
    )
    assert decision.action == "aggregate"
    assert decision.aggregate_template == "stable"


def test_dedup_outcome_change_resets_to_emit():
    decision = decide(
        context=_ctx(action="call_tool", outcome_code="success"),
        occurrence=2,
        previous_outcome_code="failure",
    )
    assert decision.action == "emit"


def test_dedup_critical_failure_is_always_emit():
    decision = decide(
        context=_ctx(action="fail"),
        occurrence=4,
        previous_outcome_code="failure",
    )
    assert decision.action == "emit"


def test_dedup_ask_user_is_always_emit():
    decision = decide(
        context=_ctx(action="ask_user"),
        occurrence=3,
        previous_outcome_code="ask",
    )
    assert decision.action == "emit"


def test_dedup_finish_is_always_emit():
    decision = decide(
        context=_ctx(action="finish"),
        occurrence=3,
        previous_outcome_code="finish",
    )
    assert decision.action == "emit"


def test_default_suppress_concise_drops_low_value():
    candidate = {
        "headline": "",
        "summary": "", "impact": "", "next_action": "",
        "details": [], "level": "info",
    }
    assert default_suppress(level="concise", candidate=candidate) is True


def test_default_suppress_concise_keeps_error_level():
    candidate = {
        "headline": "err", "summary": "x", "impact": "", "next_action": "",
        "details": [], "level": "error",
    }
    assert default_suppress(level="concise", candidate=candidate) is False


def test_default_suppress_standard_keeps_everything():
    candidate = {"headline": "ok", "summary": "", "impact": "", "next_action": ""}
    assert default_suppress(level="standard", candidate=candidate) is False
