"""Phase 2.9C narrative governance — schema and Pydantic contracts.

This module is the *data* layer; behaviour lives in ``policy``,
``signature``, ``compressor``, ``quality_validator`` and ``service``.

Schemas defined here:

* :class:`NarrativeDetailLevel` — three-mode verbosity enum.
* :class:`NarrativeContentBudget` — three budgets tied to detail level.
* :class:`NarrativeGovernanceContext` — what the service needs from
  caller (task id, update kind, route, source facts, etc).
* :class:`NarrativeSignature` — stable, transport-agnostic signature
  used by dedup / repetition counters.
* :class:`NarrativeRepetitionState` — current occurrence state for a
  signature.
* :class:`NarrativeQualityViolation` / :class:`NarrativeQualityReport`
  — gate output.
* :class:`NarrativeCostRecord` — optional LLM compression telemetry.
* :class:`NarrativeGovernanceResult` — action envelope (emit / suppress
  / aggregate / fallback) plus sanitised payload.

Forward compatibility:

* All models inherit Pydantic defaults so legacy events without
  governance fields can still be parsed as ``None`` / ``{}``.
* Sets are not used (they don't survive JSON round-trip), the wire
  variant for ``allowed_detail_keys`` is :class:`list` of strings.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ── Detail level + content budgets ──────────────────────────────────────


class NarrativeDetailLevel(str, Enum):
    """User-facing verbosity of public execution updates."""

    CONCISE = "concise"
    STANDARD = "standard"
    DETAILED = "detailed"

    @classmethod
    def parse(cls, value: Any) -> "NarrativeDetailLevel":
        """Best-effort parse; unknown values fall back to ``STANDARD``.

        Mirrors Phase 2.9C §15.4 (unknown → standard). Tests rely on
        this forgiving behaviour to keep older clients working.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            for member in cls:
                if member.value == normalized:
                    return member
        return cls.STANDARD


class NarrativeContentBudget(BaseModel):
    """Per-mode field limits. All values are upper bounds."""

    model_config = ConfigDict(frozen=False, extra="forbid")

    headline_chars: int
    summary_chars: int
    impact_chars: int
    next_action_chars: int
    detail_items: int
    detail_item_chars: int
    total_chars: int

    @classmethod
    def for_level(cls, level: NarrativeDetailLevel | str) -> "NarrativeContentBudget":
        """Return the canonical budget for the requested detail level."""
        parsed = NarrativeDetailLevel.parse(level)
        if parsed is NarrativeDetailLevel.CONCISE:
            return cls(
                headline_chars=32,
                summary_chars=120,
                impact_chars=80,
                next_action_chars=80,
                detail_items=1,
                detail_item_chars=64,
                total_chars=320,
            )
        if parsed is NarrativeDetailLevel.DETAILED:
            return cls(
                headline_chars=60,
                summary_chars=240,
                impact_chars=180,
                next_action_chars=180,
                detail_items=8,
                detail_item_chars=120,
                total_chars=720,
            )
        return cls(
            headline_chars=40,
            summary_chars=160,
            impact_chars=120,
            next_action_chars=120,
            detail_items=4,
            detail_item_chars=120,
            total_chars=480,
        )


# ── Governance context (caller-supplied) ─────────────────────────────────


class NarrativeGovernanceContext(BaseModel):
    """Caller-side contract for one ``govern()`` invocation.

    Sets aren't serialisable; ``allowed_detail_keys`` is stored as
    :class:`list` so JSON round-trips survive.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    task_internal_id: int | None = None
    conversation_id: str | None = None
    user_id: str | None = None
    user_internal_id: int | None = None

    engine_type: str | None = None
    graph_name: str | None = None
    graph_version: str | None = None
    agent_name: str | None = None
    tool_name: str | None = None
    update_kind: str | None = None

    action: str | None = None
    route: str | None = None
    phase: str | None = None
    step_index: int | None = None
    attempt: int | None = None

    detail_level: NarrativeDetailLevel = NarrativeDetailLevel.STANDARD
    source_facts: dict[str, Any] = Field(default_factory=dict)
    allowed_detail_keys: list[str] = Field(default_factory=list)
    critical: bool = False

    # Outcome hints consumed by dedup / repetition classifiers.
    outcome_code: str | None = None
    issue_codes: list[str] = Field(default_factory=list)
    artifact_version: str | None = None
    retry_strategy: str | None = None
    scope_ids: list[str] = Field(default_factory=list)


# ── Signature & repetition ──────────────────────────────────────────────


class NarrativeSignature(BaseModel):
    """Stable, transport-agnostic signature for semantic dedup.

    Signatures must derive from business facts only — never from
    timestamps, random ids, or model natural language. ``hash`` is the
    short form of ``normalized_key`` for log/UI use.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "v1"
    scope: str  # per_task / per_agent / per_tool / per_phase
    normalized_key: str
    hash: str


class NarrativeRepetitionState(BaseModel):
    """In-memory / per-task view of how often a signature has fired."""

    signature_hash: str
    occurrence: int = 1
    first_event_id: str | None = None
    previous_event_id: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    last_outcome_code: str | None = None
    improved: bool | None = None


# ── Quality gate ────────────────────────────────────────────────────────


class NarrativeQualityViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["warning", "block"]
    field: str | None = None
    message: str


class NarrativeQualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    violations: list[NarrativeQualityViolation] = Field(default_factory=list)
    repaired: bool = False
    fallback_used: bool = False
    sanitizer_applied: bool = False


# ── Cost accounting ─────────────────────────────────────────────────────


class NarrativeCostRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deterministic_chars_in: int = 0
    deterministic_chars_out: int = 0
    llm_compression_called: bool = False
    llm_model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    cache_hit: bool = False
    budget_blocked: bool = False
    usage_available: bool = True


# ── Result envelope ─────────────────────────────────────────────────────


GovernanceAction = Literal["emit", "suppress", "aggregate", "fallback"]


class NarrativeGovernanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: GovernanceAction
    public_update: dict[str, Any] | None = None
    detail_level: NarrativeDetailLevel = NarrativeDetailLevel.STANDARD
    signature: NarrativeSignature | None = None
    repetition_count: int = 1
    quality_report: NarrativeQualityReport
    cost: NarrativeCostRecord = Field(default_factory=NarrativeCostRecord)
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "NarrativeDetailLevel",
    "NarrativeContentBudget",
    "NarrativeGovernanceContext",
    "NarrativeSignature",
    "NarrativeRepetitionState",
    "NarrativeQualityViolation",
    "NarrativeQualityReport",
    "NarrativeCostRecord",
    "GovernanceAction",
    "NarrativeGovernanceResult",
]
# narrative_governance.schemas:Phase 2.9C 治理层数据契约(DetailLevel / Budget / Context / Decision / ServiceResult)。
