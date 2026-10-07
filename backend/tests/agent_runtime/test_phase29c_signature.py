"""Phase 2.9C narrative signature — determinism + secret-free tests."""

from __future__ import annotations

from app.agent_runtime._shared.narrative_governance.schemas import (
    NarrativeGovernanceContext,
)
from app.agent_runtime._shared.narrative_governance.signature import (
    _canonicalize,
    build_signature,
)


def _ctx(**overrides) -> NarrativeGovernanceContext:
    base = {
        "task_id": "t1",
        "agent_name": "PreparationAgent",
        "update_kind": "agent_decision_update",
        "tool_name": "KnowledgeSearchTool",
        "action": "finish",
        "route": "finish",
    }
    base.update(overrides)
    return NarrativeGovernanceContext.model_validate(base)


def test_signature_two_identical_inputs_are_equal():
    candidate = {"headline": "ok", "summary": "ok", "details": []}
    sig_a = build_signature(candidate, _ctx())
    sig_b = build_signature(candidate, _ctx())
    assert sig_a.hash == sig_b.hash
    assert sig_a.normalized_key == sig_b.normalized_key


def test_signature_route_change_yields_different_hash():
    candidate = {"headline": "ok", "summary": "ok"}
    sig_a = build_signature(candidate, _ctx(route="finish"))
    sig_b = build_signature(candidate, _ctx(route="repair"))
    assert sig_a.hash != sig_b.hash


def test_signature_issue_code_change_yields_different_hash():
    candidate = {"headline": "ok", "summary": "ok"}
    sig_a = build_signature(candidate, _ctx(issue_codes=[]))
    sig_b = build_signature(candidate, _ctx(issue_codes=["BOOKMARK_LOSS"]))
    assert sig_a.hash != sig_b.hash


def test_signature_ignores_legacy_display_keys():
    candidate = {
        "headline": "ok",
        "summary": "ok",
        "details": ["d1"],
        "timestamp": "2026-01-01T00:00:00",
        "event_id": "evt-001",
        "public_id": "pub-001",
    }
    sig_a = build_signature(candidate, _ctx())
    sig_b = build_signature(
        {"headline": "ok", "summary": "ok", "details": ["d1"]},
        _ctx(),
    )
    assert sig_a.hash == sig_b.hash


def test_signature_outcome_change_yields_different_hash():
    candidate = {"headline": "ok", "summary": "ok"}
    sig_a = build_signature(candidate, _ctx(outcome_code="success"))
    sig_b = build_signature(candidate, _ctx(outcome_code="failure"))
    assert sig_a.hash != sig_b.hash


def test_signature_does_not_carry_sensitive_strings_from_candidate():
    """The signature is built from a small allow-list of business
    fields; it must not absorb arbitrary free-form strings the
    candidate may carry (including potential prompt-injection
    payloads or sensitive tokens).
    """
    candidate = {
        "headline": "ok",
        "summary": "ok",
        "secondary": "Bearer abc.def.ghi",
    }
    sig = build_signature(candidate, _ctx())
    candidate2 = {
        "headline": "ok",
        "summary": "ok",
        "secondary": "Bearer different.token.value",
    }
    sig2 = build_signature(candidate2, _ctx())
    assert sig.hash == sig2.hash


def test_signature_business_facts_change_hash():
    candidate = {"headline": "ok", "summary": "ok", "retry_count": 1}
    sig_a = build_signature(candidate, _ctx())
    sig_b = build_signature(candidate, _ctx(retry_strategy="retry_with_backoff"))
    assert sig_a.hash != sig_b.hash


def test_canonicalize_strips_known_display_keys():
    payload = {
        "headline": "ignore",
        "timestamp": "2026",
        "event_id": "x",
        "details": [{"foo": 1}],
    }
    canonical = _canonicalize(payload)
    assert "headline" not in canonical
    assert "timestamp" not in canonical
    assert "event_id" not in canonical


def test_canonicalize_sorts_lists_for_determinism():
    a = _canonicalize({"x": [3, 1, 2]})
    b = _canonicalize({"x": [2, 3, 1]})
    assert a == b
