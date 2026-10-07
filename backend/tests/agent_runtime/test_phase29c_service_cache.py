"""Phase 2.9C governance service + cache tests."""

from __future__ import annotations

import pytest

from app.agent_runtime._shared.narrative_governance.cache import (
    Budget,
    NarrativeCompressionCache,
    hash_facts,
    stub_compress,
)
from app.agent_runtime._shared.narrative_governance.schemas import (
    NarrativeDetailLevel,
    NarrativeGovernanceContext,
    NarrativeRepetitionState,
)
from app.agent_runtime._shared.narrative_governance.service import (
    NarrativeGovernanceService,
)


def _ctx(**kw) -> NarrativeGovernanceContext:
    base = {
        "task_id": "t1",
        "agent_name": "PreparationAgent",
        "update_kind": "agent_decision_update",
        "action": "call_tool",
        "route": "execute_tool",
        "outcome_code": "success",
    }
    base.update(kw)
    return NarrativeGovernanceContext.model_validate(base)


# ── Cache / budget / stub ───────────────────────────────────────


def test_cache_set_and_get_round_trip():
    cache = NarrativeCompressionCache()
    cache.set("k1", {"payload": {"a": 1}})
    assert cache.get("k1") == {"payload": {"a": 1}}


def test_cache_eviction_keeps_max_entries():
    cache = NarrativeCompressionCache(max_entries=2)
    cache.set("a", {"v": 1})
    cache.set("b", {"v": 2})
    cache.set("c", {"v": 3})
    assert cache.get("a") is None
    assert cache.get("b") == {"v": 2}
    assert cache.get("c") == {"v": 3}


def test_cache_build_key_includes_required_fields():
    cache = NarrativeCompressionCache()
    base_key = cache.build_key(
        version="v1",
        detail_level="standard",
        normalized_fact_hash="abc",
    )
    other_key = cache.build_key(
        version="v1",
        detail_level="concise",
        normalized_fact_hash="abc",
    )
    assert base_key != other_key


def test_hash_facts_deterministic_for_same_input():
    h1 = hash_facts({"a": 1, "b": [3, 2, 1]})
    h2 = hash_facts({"a": 1, "b": [3, 2, 1]})
    assert h1 == h2


def test_hash_facts_different_for_different_input():
    h1 = hash_facts({"a": 1})
    h2 = hash_facts({"a": 2})
    assert h1 != h2


def test_budget_can_call_respects_max_calls():
    budget = Budget(max_calls_per_task=1)
    assert budget.can_call() is True
    budget.record_call(
        prompt_tokens=0, completion_tokens=0, latency_ms=0, cache_hit=False
    )
    assert budget.can_call() is False
    assert budget.stats.budget_blocked == 1


def test_stub_compress_returns_payload_when_budget_allows():
    cache = NarrativeCompressionCache()
    budget = Budget(max_calls_per_task=2)
    result = stub_compress(
        {"headline": "ok", "summary": "x" * 200},
        level="standard",
        budget=budget,
        cache=cache,
        fact_hash="abc",
    )
    assert result is not None
    assert "headline" in result.payload
    assert budget.stats.calls == 1


def test_stub_compress_serves_cache_on_second_call():
    cache = NarrativeCompressionCache()
    budget = Budget(max_calls_per_task=2)
    candidate = {"headline": "ok", "summary": "x" * 200}
    stub_compress(
        candidate,
        level="standard",
        budget=budget,
        cache=cache,
        fact_hash="hash-1",
    )
    before = budget.stats.calls
    second = stub_compress(
        candidate,
        level="standard",
        budget=budget,
        cache=cache,
        fact_hash="hash-1",
    )
    assert second is not None
    assert second.cache_hit is True
    # The cache hit still records in stats (cache_hits++) but is
    # typically counted as a call.
    assert budget.stats.cache_hits >= 1


def test_stub_compress_returns_none_when_budget_exhausted():
    cache = NarrativeCompressionCache()
    budget = Budget(max_calls_per_task=0)
    result = stub_compress(
        {"headline": "ok"},
        level="standard",
        budget=budget,
        cache=cache,
    )
    assert result is None
    assert budget.stats.budget_blocked == 1


# ── Governance service ────────────────────────────────────────────


def test_service_first_occurrence_emits_with_metadata():
    svc = NarrativeGovernanceService()
    result = svc.govern(
        candidate={
            "headline": "ok",
            "summary": "a brief summary",
            "impact": "moderate",
            "next_action": "continue",
            "level": "info",
        },
        context=_ctx(),
    )
    assert result.action == "emit"
    assert result.public_update is not None
    assert "governance_metadata" in result.public_update
    meta = result.public_update["governance_metadata"]
    assert meta["version"] == "2.9C-v1"
    assert meta["applied"] is True


def test_service_second_occurrence_aggregates():
    svc = NarrativeGovernanceService()
    ctx = _ctx()
    candidate = {
        "headline": "ok", "summary": "ok", "impact": "", "next_action": "",
        "details": [], "level": "info",
    }
    svc.govern(candidate=candidate, context=ctx)
    # Second time with same signature → aggregate.
    result = svc.govern(
        candidate=candidate,
        context=ctx,
        repetition=NarrativeRepetitionState(
            signature_hash="x", occurrence=2, last_outcome_code="success",
        ),
    )
    assert result.action == "aggregate"
    assert result.metadata.get("aggregate_template") == "repeat"


def test_service_third_occurrence_escalates():
    svc = NarrativeGovernanceService()
    result = svc.govern(
        candidate={
            "headline": "ok", "summary": "ok", "impact": "", "next_action": "",
            "details": [], "level": "info",
        },
        context=_ctx(),
        repetition=NarrativeRepetitionState(
            signature_hash="x", occurrence=3, last_outcome_code="success",
        ),
    )
    assert result.action == "aggregate"
    assert result.metadata.get("aggregate_template") == "stable"


def test_service_concise_level_shortens_payload():
    svc = NarrativeGovernanceService()
    result = svc.govern(
        candidate={
            "headline": "ok",
            "summary": "x" * 400,
            "impact": "y" * 200,
            "next_action": "n" * 200,
            "details": ["d1", "d2", "d3"],
        },
        context=_ctx(detail_level="concise"),
    )
    assert result.action == "emit"
    assert len(result.public_update["summary"]) <= 120
    assert len(result.public_update["headline"]) <= 32


def test_service_propagates_quality_violations():
    svc = NarrativeGovernanceService()
    result = svc.govern(
        candidate={
            "headline": "headline with 99 specific number",
            "summary": "ok", "impact": "", "next_action": "", "details": [],
        },
        context=_ctx(source_facts={"hit_count": 5}),
    )
    assert result.quality_report.violations
    codes = {v.code for v in result.quality_report.violations}
    assert "unsupported_number" in codes


def test_service_swallows_errors_into_fallback_action():
    svc = NarrativeGovernanceService()
    # Pass a non-dict candidate with valid context — should not raise.
    result = svc.govern(candidate=None, context=_ctx())
    assert result.action in {"emit", "fallback", "suppress", "aggregate"}


def test_service_knows_signature():
    svc = NarrativeGovernanceService()
    result = svc.govern(
        candidate={"headline": "ok", "summary": "ok"},
        context=_ctx(),
    )
    assert result.signature is not None
    assert result.signature.hash
    assert result.signature.normalized_key


def test_service_detail_level_default_is_standard():
    svc = NarrativeGovernanceService()
    result = svc.govern(
        candidate={"headline": "ok", "summary": "ok"},
        context=_ctx(),
    )
    assert result.detail_level is NarrativeDetailLevel.STANDARD
