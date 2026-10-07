"""Phase 2.9C semantic dedup + repetition escalation.

DedupPolicy implements Phase 2.9C §9.3:

* First occurrence → ``emit`` with the full candidate.
* Second occurrence with same outcome → ``aggregate`` with the
  :class:`AggFirst` template (still no improvement).
* Third+ occurrence with the same outcome → :class:`AggStable`
  escalation (suggests stopping auto-retry).
* Route / outcome changes → ``emit`` (no dedup).
* Critical / fail / ask_user / terminal candidates are always
  ``emit`` even if signature repeats (Phase 2.9C §9.4).

The classifier is purely deterministic; the only state it consumes
is the current ``occurrence`` count and the previous outcome code.
History Replay does NOT call this classifier — events replay from
the persisted payload, which already records ``repetition_count`` and
``governance_action``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.agent_runtime._shared.narrative_governance.schemas import (
    GovernanceAction,
    NarrativeGovernanceContext,
)


@dataclass(frozen=True)
class DedupDecision:
    action: GovernanceAction
    aggregate_template: str  # "first" | "repeat" | "stable" | ""
    reason: str


_CRITICAL_KINDS = frozenset(
    {
        # ask / failure / completion / error / format-loss are
        # semantically "must show", so dedup never aggregates them.
        "preparation_fallback",
        "repair_fallback",
        "incremental_fallback",
        "ask_user",
        "need_user_confirm",
        "tool_failed",
        "task_failed",
        "task_completed",
        "artifact_unavailable",
        "format_loss_confirm_requested",
        "docx_format_checked",
    }
)


def _is_critical(context: NarrativeGovernanceContext, candidate: dict[str, Any] | None) -> bool:
    if context.critical:
        return True
    action = (context.action or "").lower()
    if action in {"fail", "ask_user"}:
        return True
    # ``finish`` is critical (terminal state) but ``call_tool`` repeats are
    # exactly what we want to dedup.
    kind = (context.update_kind or "").lower()
    if kind in _CRITICAL_KINDS:
        return True
    if isinstance(candidate, dict):
        level = str(candidate.get("level") or "").lower()
        if level == "error":
            return True
        if bool(candidate.get("critical")):
            return True
    return False


def _outcome_changed(context: NarrativeGovernanceContext, previous: str | None) -> bool:
    """True if the route / outcome / artifact version diverged from memory."""
    if previous is None:
        return False
    current = context.outcome_code or ""
    if not current or not previous:
        # Without a stable outcome code in either side we don't claim
        # "changed" — we only know that the candidate repeated.
        return False
    return previous != current


def decide(
    *,
    context: NarrativeGovernanceContext,
    occurrence: int,
    previous_outcome_code: str | None,
    candidate: dict[str, Any] | None = None,
) -> DedupDecision:
    """Classify one candidate against the dedup state.

    Args:
        context: governance context (route / outcome / agent / kind /
            critical).
        occurrence: how many times this signature has been seen *in
            the current process / task window*, **including** the
            incoming candidate (i.e. ``occurrence >= 1``).
        previous_outcome_code: outcome code from the previous
            occurrence (or ``None`` for the first observation).
        candidate: optional raw public-update payload (for critical
            detection).
    """
    critical = _is_critical(context, candidate)
    if critical:
        return DedupDecision(
            action="emit",
            aggregate_template="",
            reason="critical_event_must_not_be_suppressed",
        )

    action = (context.action or "").lower()
    # ``finish`` is a terminal state — never aggregate it, even if
    # the same signature happens to have repeated before.
    if action == "finish":
        return DedupDecision(
            action="emit",
            aggregate_template="",
            reason="terminal_finish_event",
        )

    if occurrence <= 1:
        return DedupDecision(
            action="emit",
            aggregate_template="",
            reason="first_occurrence",
        )

    if _outcome_changed(context, previous_outcome_code):
        return DedupDecision(
            action="emit",
            aggregate_template="",
            reason="outcome_or_route_changed",
        )

    if occurrence == 2:
        return DedupDecision(
            action="aggregate",
            aggregate_template="repeat",
            reason="second_occurrence_repeat",
        )

    # occurrence >= 3 → escalate (stable pattern)
    return DedupDecision(
        action="aggregate",
        aggregate_template="stable",
        reason="third_or_later_occurrence_stable",
    )


def default_suppress(
    *,
    level: str,
    candidate: dict[str, Any] | None,
) -> bool:
    """Decide whether to suppress an *otherwise low-value* duplicate.

    Concise mode aggressively suppresses ordinary successful events;
    standard / detailed modes are conservative. Critical candidates
    bypass suppression entirely (``decide()`` returns first).
    """
    if level == "concise":
        if not isinstance(candidate, dict):
            return False
        is_err = str(candidate.get("level") or "").lower() == "error"
        has_impact = bool(str(candidate.get("impact") or "").strip())
        has_next = bool(str(candidate.get("next_action") or "").strip())
        has_headline = bool(str(candidate.get("headline") or "").strip())
        # Only suppress when the candidate is genuinely empty:
        # no headline + no impact + no next_action + not errored.
        return not (is_err or has_impact or has_next or has_headline)
    return False


__all__ = ["DedupDecision", "decide", "default_suppress"]
# narrative_governance.dedup:Phase 2.9C 语义去重 + 重复升级(emit → aggregate → AggStable);路由/结果变更 → emit。
