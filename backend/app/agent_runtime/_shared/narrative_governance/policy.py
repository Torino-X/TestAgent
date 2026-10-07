"""Phase 2.9C narrative detail policy — three-mode projection.

Implements Phase 2.9C §8. The ``NarrativePolicyResolver`` resolves
detail level from context. ``NarrativeDetailProjector`` projects an
existing candidate into a new payload that obeys the resolved
``NarrativeContentBudget`` and the critical-event whitelist.

The projector never creates new text. It can:

* Drop fields (e.g. ``impact`` or ``next_action`` at concise).
* Truncate text.
* Reduce list length with a "…N more" footnote.
* Mark the candidate for suppression (returning ``None``).

It does NOT alter fact claims — that is the ``quality_validator``'s
job. The pipeline order in Phase 2.9C §11.1 is respected by the
:mod:`service` module that wires policy → quality → emission.
"""

from __future__ import annotations

from typing import Any

from app.agent_runtime._shared.narrative_governance.dedup import (
    _is_critical,
    default_suppress,
)
from app.agent_runtime._shared.narrative_governance.schemas import (
    NarrativeContentBudget,
    NarrativeDetailLevel,
    NarrativeGovernanceContext,
)


def resolve_level(context: NarrativeGovernanceContext) -> NarrativeDetailLevel:
    """Pick the detail level for the current governance invocation.

    Priority (Phase 2.9C §8.1):
        task override > user setting > environment default > standard.

    The caller (``governance_service``) is expected to inject the
    resolved user setting into ``context.detail_level`` before
    calling this function. When we get here, the level is already
    normalised so this resolver only enforces that unknown values
    fall back to STANDARD.
    """
    return NarrativeDetailLevel.parse(context.detail_level)


def _truncate(text: Any, limit: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 1:
        return cleaned[:limit]
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def _project_text(value: Any, budget: NarrativeContentBudget, field: str) -> str:
    limit = {
        "headline": budget.headline_chars,
        "summary": budget.summary_chars,
        "impact": budget.impact_chars,
        "next_action": budget.next_action_chars,
    }.get(field, budget.summary_chars)
    return _truncate(value, limit)


def _project_details(details: Any, budget: NarrativeContentBudget) -> list[str]:
    if details is None:
        return []
    if isinstance(details, str):
        return [_truncate(details, budget.detail_item_chars)]
    if not isinstance(details, list):
        return []
    projected: list[str] = []
    for item in details[: budget.detail_items]:
        projected.append(_truncate(item, budget.detail_item_chars))
    if len(details) > budget.detail_items:
        projected.append(f"…其余 {len(details) - budget.detail_items} 项已省略")
    return projected


def project(
    candidate: dict[str, Any] | None,
    *,
    context: NarrativeGovernanceContext,
) -> dict[str, Any] | None:
    """Project ``candidate`` for the requested detail level.

    Returns ``None`` if the candidate should be suppressed entirely
    (only allowed for low-value duplicates at the most aggressive
    setting).
    """
    if not isinstance(candidate, dict):
        return None

    level = resolve_level(context)
    budget = NarrativeContentBudget.for_level(level)

    # Concise mode aggressively suppresses low-value events but never
    # critical / fail / ask_user / completed.
    if level is NarrativeDetailLevel.CONCISE and default_suppress(
        level=level.value,
        candidate=candidate,
    ):
        if _is_critical(context, candidate):
            pass  # critical: still project (truncated)
        elif (context.action or "").lower() not in {"fail", "ask_user", "finish"}:
            return None

    projected = dict(candidate)
    projected["headline"] = _project_text(
        candidate.get("headline"), budget, "headline"
    )
    # summary / impact / next_action are dropped when empty OR
    # concise (we keep them so the wire format stays consistent).
    summary_value = _project_text(
        candidate.get("summary"), budget, "summary"
    )
    projected["summary"] = summary_value
    if level is NarrativeDetailLevel.CONCISE and not summary_value:
        # Concise needs a one-liner minimum — caller is still allowed
        # to keep ``headline`` only, that's acceptable.
        pass
    projected["impact"] = _project_text(
        candidate.get("impact"), budget, "impact"
    )
    projected["next_action"] = _project_text(
        candidate.get("next_action"), budget, "next_action"
    )
    projected["details"] = _project_details(candidate.get("details"), budget)
    projected["detail_level"] = level.value

    # Sanity: total characters cap.
    aggregate = (
        projected["headline"]
        + projected.get("summary", "")
        + projected.get("impact", "")
        + projected.get("next_action", "")
    )
    joined_details = "\n".join(projected["details"])
    total = len(aggregate) + len(joined_details)
    if total > budget.total_chars and joined_details:
        # Drop details when overflowing; the text is already short.
        projected["details"] = []

    return projected


__all__ = ["resolve_level", "project"]
