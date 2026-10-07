"""Phase 2.9C narrative governance — schema + parse tests."""

from __future__ import annotations

import json

import pytest

from app.agent_runtime._shared.narrative_governance.schemas import (
    NarrativeContentBudget,
    NarrativeCostRecord,
    NarrativeDetailLevel,
    NarrativeGovernanceContext,
    NarrativeGovernanceResult,
    NarrativeQualityReport,
    NarrativeQualityViolation,
    NarrativeRepetitionState,
    NarrativeSignature,
)


def test_detail_level_parse_unknown_falls_back_to_standard():
    assert NarrativeDetailLevel.parse("verbose") is NarrativeDetailLevel.STANDARD
    assert NarrativeDetailLevel.parse("") is NarrativeDetailLevel.STANDARD
    assert NarrativeDetailLevel.parse(None) is NarrativeDetailLevel.STANDARD


def test_detail_level_parse_recognised_values():
    assert NarrativeDetailLevel.parse("concise") is NarrativeDetailLevel.CONCISE
    assert NarrativeDetailLevel.parse("STANDARD") is NarrativeDetailLevel.STANDARD
    assert NarrativeDetailLevel.parse("detailed") is NarrativeDetailLevel.DETAILED


def test_budget_isolated_per_level():
    concise = NarrativeContentBudget.for_level("concise")
    standard = NarrativeContentBudget.for_level("standard")
    detailed = NarrativeContentBudget.for_level("detailed")
    assert concise.headline_chars < standard.headline_chars < detailed.headline_chars
    assert standard.summary_chars <= detailed.summary_chars


def test_budget_default_for_level_uses_parse_safe_input():
    fallback = NarrativeContentBudget.for_level(None)
    assert fallback.headline_chars == 40


def test_governance_context_round_trip_json():
    ctx = NarrativeGovernanceContext(
        task_id="t1",
        task_internal_id=42,
        agent_name="PreparationAgent",
        update_kind="agent_decision_update",
        action="finish",
        route="finish",
        outcome_code="ok",
        issue_codes=["a", "b"],
        scope_ids=["x"],
        allowed_detail_keys=["hit_count"],
        critical=True,
    )
    payload = ctx.model_dump()
    rebuilt = NarrativeGovernanceContext.model_validate(payload)
    assert rebuilt.agent_name == "PreparationAgent"
    assert rebuilt.issue_codes == ["a", "b"]


def test_governance_context_extra_forbid():
    with pytest.raises(Exception):
        NarrativeGovernanceContext.model_validate(
            {"task_id": "t1", "extra_unknown_field": "x"}
        )


def test_governance_context_legacy_payload_passes():
    """Existing 2.9A / 2.9B payloads without 2.9C fields must still
    validate (forward compatible).
    """
    legacy = {
        "task_id": "t1",
        "update_kind": "tool_finished",
        "agent_name": "PreparationAgent",
    }
    ctx = NarrativeGovernanceContext.model_validate(legacy)
    assert ctx.detail_level is NarrativeDetailLevel.STANDARD
    assert ctx.allowed_detail_keys == []


def test_signature_is_frozen_and_pydantic_v2():
    sig = NarrativeSignature(
        version="v1", scope="per_task", normalized_key="x", hash="abcd1234",
    )
    with pytest.raises(Exception):
        sig.hash = "tampered"  # noqa: F841


def test_repetition_state_default_occurrence_one():
    state = NarrativeRepetitionState(signature_hash="abcd1234")
    assert state.occurrence == 1
    assert state.previous_event_id is None


def test_quality_report_default_passes():
    report = NarrativeQualityReport(passed=True)
    assert report.repaired is False
    assert report.sanitizer_applied is False
    assert report.violations == []


def test_quality_violation_extra_forbid():
    with pytest.raises(Exception):
        NarrativeQualityViolation(
            code="x", severity="block", message="m", extra_bad=True,
        )


def test_cost_record_defaults_to_no_op():
    cost = NarrativeCostRecord()
    assert cost.llm_compression_called is False
    assert cost.cache_hit is False
    assert cost.budget_blocked is False
    assert cost.usage_available is True
    assert cost.prompt_tokens is None


def test_governance_result_action_must_be_known_literal():
    with pytest.raises(Exception):
        NarrativeGovernanceResult.model_validate(
            {
                "action": "explode",  # invalid literal
                "quality_report": {"passed": True},
                "detail_level": "standard",
            }
        )


def test_json_round_trip_of_governance_result():
    payload = {
        "action": "emit",
        "public_update": {"headline": "h", "summary": "s"},
        "detail_level": "standard",
        "signature": {"version": "v1", "scope": "per_task", "normalized_key": "x", "hash": "abcd1234"},
        "repetition_count": 2,
        "quality_report": {"passed": True, "violations": []},
        "cost": {"deterministic_chars_in": 100, "deterministic_chars_out": 80},
    }
    result = NarrativeGovernanceResult.model_validate(payload)
    json.dumps(result.model_dump())
    assert result.action == "emit"
    assert result.cost.deterministic_chars_in == 100
