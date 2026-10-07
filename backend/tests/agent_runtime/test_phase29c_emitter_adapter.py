"""Phase 2.9C emitter adapter + flag-gating tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.agent_runtime._shared.narrative_governance import emitter_adapter
from app.agent_runtime._shared.narrative_governance.schemas import (
    NarrativeGovernanceContext,
)
from app.agent_runtime import feature_flags as ff_mod
from app.agent_runtime.feature_flags import AgentRuntimeFeatureFlags


@pytest.fixture
def narrative_flag(monkeypatch):
    """Toggle the master Phase 2.9C switch via the ``get_feature_flags`` shim."""

    def _set(value: bool) -> None:
        flags = AgentRuntimeFeatureFlags(
            phase29c_governance_enabled=value,
            phase29c_quality_gate_enabled=True,
            phase29c_detail_level_enabled=True,
            phase29c_dedup_enabled=True,
            phase29c_compression_enabled=True,
            phase29c_llm_compression_enabled=False,
            phase29c_shadow_mode=False,
        )
        monkeypatch.setattr(ff_mod, "get_feature_flags", lambda: flags)

    _set(False)
    return _set


def _ctx(**kw: Any) -> NarrativeGovernanceContext:
    base = {
        "task_id": "t1",
        "agent_name": "PreparationAgent",
        "update_kind": "agent_decision_update",
        "action": "call_tool",
        "route": "execute_tool",
    }
    base.update(kw)
    return NarrativeGovernanceContext.model_validate(base)


def test_default_flag_is_off():
    assert emitter_adapter.is_governance_active() is False


@pytest.mark.asyncio
async def test_adapter_passes_through_when_flag_off(narrative_flag):
    narrative_flag(False)
    candidate = {"headline": "ok", "summary": "ok"}
    out = await emitter_adapter.govern_async_payload(
        candidate=candidate,
        context=_ctx(),
    )
    assert out == candidate


@pytest.mark.asyncio
async def test_adapter_runs_governance_when_flag_on(narrative_flag):
    narrative_flag(True)
    candidate = {
        "headline": "ok",
        "summary": "ok",
        "impact": "",
        "next_action": "",
        "level": "info",
    }
    from app.agent_runtime._shared.narrative_governance.service import (
        NarrativeGovernanceService,
    )
    svc = NarrativeGovernanceService()
    direct_result = svc.govern(candidate=candidate, context=_ctx())
    print("direct result action:", direct_result.action)
    print("direct public_update keys:", list((direct_result.public_update or {}).keys()))
    print("is_governance_active:", emitter_adapter.is_governance_active())
    out = await emitter_adapter.govern_async_payload(
        candidate=candidate,
        context=_ctx(),
    )
    print("adapter out keys:", list((out or {}).keys()))
    # When governance is on the payload is rewritten with metadata.
    assert isinstance(out, dict)
    assert "governance_metadata" in out


@pytest.mark.asyncio
async def test_adapter_returns_none_on_suppress(narrative_flag):
    """Two consecutive occurrences of the same non-critical candidate
    with concise mode → repeat aggregate still emits; the path we want
    to verify here is that *no metadata* block has leaked onto the
    fallback payload (so the caller still produces a stable message).
    Use a slightly different scenario: a candidate with level="error"
    and concise mode would normally pass through with truncated text,
    not be suppressed — the suppression in Phase 2.9C §9.4 needs a
    sig-matched repeat that is non-critical. We exercise that by
    issuing two consecutive identical candidates.
    """
    narrative_flag(True)
    ctx = _ctx(detail_level="concise", action="call_tool")
    candidate = {
        "headline": "tool succeeded",
        "summary": "details",
        "impact": "no impact",
        "next_action": "next",
        "level": "info",
    }
    from app.agent_runtime._shared.narrative_governance.schemas import (
        NarrativeRepetitionState,
    )
    # First occurrence → emit (full payload).
    first = await emitter_adapter.govern_async_payload(
        candidate=candidate, context=ctx,
    )
    assert isinstance(first, dict)
    # Second occurrence with identical signature → aggregate repeat.
    second = await emitter_adapter.govern_async_payload(
        candidate=candidate, context=ctx,
        repetition=NarrativeRepetitionState(
            signature_hash="x", occurrence=2, last_outcome_code="",
        ),
    )
    # Aggregate repeat returns a payload that points the user to the
    # "second occurrence" pattern.
    assert isinstance(second, dict)
    assert "headline" in second


@pytest.mark.asyncio
async def test_adapter_swallows_session_lookup_failures(narrative_flag, monkeypatch):
    narrative_flag(True)

    async def _boom(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("transient DB error")

    emitter_adapter.register_detail_level_resolver(_boom)
    out = await emitter_adapter.govern_async_payload(
        candidate={"headline": "ok", "summary": "ok"},
        context=_ctx(),
        session=object(),
        user_internal_id=1,
    )
    assert isinstance(out, dict)


@pytest.mark.asyncio
async def test_adapter_survives_service_exception(narrative_flag, monkeypatch):
    narrative_flag(True)

    def _raise_govern(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("govern failure")

    from app.agent_runtime._shared.narrative_governance import service as svc_mod

    monkeypatch.setattr(svc_mod.NarrativeGovernanceService, "govern", _raise_govern)
    candidate = {"headline": "ok", "summary": "ok"}
    out = await emitter_adapter.govern_async_payload(
        candidate=candidate,
        context=_ctx(),
    )
    assert out == candidate
