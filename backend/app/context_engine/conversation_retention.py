"""Shared conversation-retention policy for Context Engine composition.

The product keeps a wider verbatim window during normal chat, then folds only
the older half of that window during manual compaction.  A turn starts at a
user message and includes the assistant messages that follow it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence, TypeVar


# Before the working context reaches its proactive compaction waterline, the
# whole conversation remains verbatim.  Once a durable summary exists, the
# exact tail remains twenty complete turns.  Keeping the same value on both
# sides avoids the surprising "manual compaction cut my raw window in half"
# behaviour that previously retained only ten turns.
NORMAL_RECENT_TURNS = 20
"""Light retention: preserve the newest twenty complete turns verbatim."""

DEEP_RECENT_TURNS = 12
"""Deep retention: preserve twelve complete turns after a quick re-growth."""

POST_COMPACTION_MIN_RECENT_TURNS = NORMAL_RECENT_TURNS
EXPECTED_MESSAGES_PER_TURN = 2

AUTO_LIGHT_TARGET_RATIO = 0.55
AUTO_DEEP_TARGET_RATIO = 0.52
MANUAL_LIGHT_SOURCE_RATIO = 0.30
MANUAL_DEEP_SOURCE_RATIO = 0.20

# ``ConversationSummary.schema_version`` already travels with the durable
# summary row, so it is the safest backwards-compatible place to preserve the
# tail contract selected by compaction.  Older summaries remain ``v1`` and
# therefore retain the historical twenty-turn behaviour.
SUMMARY_SCHEMA_VERSION_LIGHT = "v2-retention-light"
SUMMARY_SCHEMA_VERSION_DEEP = "v2-retention-deep"


@dataclass(frozen=True)
class ConversationRetentionDecision:
    """The durable, auditable retention tier selected for one compaction."""

    level: Literal["light", "deep"]
    keep_turns: int
    target_ratio: float
    policy_key: str


def count_complete_turns(messages: Sequence[object]) -> int:
    """Count user-anchored turns, with a safe legacy row-count fallback."""
    ordered = list(messages)
    user_turns = sum(1 for message in ordered if getattr(message, "role", None) == "user")
    if user_turns:
        return user_turns
    return (len(ordered) + EXPECTED_MESSAGES_PER_TURN - 1) // EXPECTED_MESSAGES_PER_TURN


def choose_retention_decision(
    *,
    has_prior_summary: bool,
    newly_compactable_turns: int,
    manual: bool = False,
) -> ConversationRetentionDecision:
    """Choose light/deep compaction without using wall-clock time.

    The first automatic retention pass is light.  Once a durable summary
    exists, the next automatic threshold crossing is deep: the previous
    light pass has already preserved its wider twenty-turn raw window, so a
    second crossing needs a twelve-turn tail to create meaningful headroom.

    ``newly_compactable_turns`` remains part of the shared call contract for
    telemetry-compatible callers, but it must not downgrade the second
    crossing merely because the first light pass happened to reclaim more
    than its nominal low-water target.
    """
    del newly_compactable_turns
    deep = has_prior_summary
    if deep:
        return ConversationRetentionDecision(
            level="deep",
            keep_turns=DEEP_RECENT_TURNS,
            target_ratio=MANUAL_DEEP_SOURCE_RATIO if manual else AUTO_DEEP_TARGET_RATIO,
            policy_key="conversation-retention.deep.v2",
        )
    return ConversationRetentionDecision(
        level="light",
        keep_turns=NORMAL_RECENT_TURNS,
        target_ratio=MANUAL_LIGHT_SOURCE_RATIO if manual else AUTO_LIGHT_TARGET_RATIO,
        policy_key="conversation-retention.light.v2",
    )


def recent_turns_for_summary_schema(schema_version: object) -> int:
    """Read a durable tail contract while keeping legacy summaries stable."""
    return (
        DEEP_RECENT_TURNS
        if str(schema_version or "") == SUMMARY_SCHEMA_VERSION_DEEP
        else NORMAL_RECENT_TURNS
    )


def summary_schema_version_for_policy(policy_key: object) -> str:
    """Map a compaction audit policy to its durable summary contract."""
    return (
        SUMMARY_SCHEMA_VERSION_DEEP
        if str(policy_key or "") == "conversation-retention.deep.v2"
        else SUMMARY_SCHEMA_VERSION_LIGHT
    )

_MessageT = TypeVar("_MessageT")


def split_before_recent_turns(
    messages: Sequence[_MessageT],
    *,
    keep_turns: int,
) -> tuple[list[_MessageT], list[_MessageT]]:
    """Split chronological messages before the newest ``keep_turns`` turns.

    User messages define turn boundaries.  The fallback to two rows per turn
    handles legacy or partially imported timelines that have no usable user
    role markers.
    """
    ordered = list(messages)
    if not ordered or keep_turns <= 0:
        return ordered, []

    user_indexes = [
        index
        for index, message in enumerate(ordered)
        if getattr(message, "role", None) == "user"
    ]
    if not user_indexes:
        keep_messages = keep_turns * EXPECTED_MESSAGES_PER_TURN
        boundary = max(0, len(ordered) - keep_messages)
        return ordered[:boundary], ordered[boundary:]
    if len(user_indexes) <= keep_turns:
        return [], ordered

    boundary = user_indexes[-keep_turns]
    return ordered[:boundary], ordered[boundary:]


def recent_turn_tail(
    messages: Sequence[_MessageT],
    *,
    keep_turns: int,
) -> list[_MessageT]:
    """Return the newest complete turn region from chronological messages."""
    _older, recent = split_before_recent_turns(messages, keep_turns=keep_turns)
    return recent


__all__ = [
    "AUTO_DEEP_TARGET_RATIO",
    "AUTO_LIGHT_TARGET_RATIO",
    "ConversationRetentionDecision",
    "DEEPEN_AFTER_NEWLY_COMPACTABLE_TURNS_AT_MOST",
    "DEEP_RECENT_TURNS",
    "EXPECTED_MESSAGES_PER_TURN",
    "MANUAL_DEEP_SOURCE_RATIO",
    "MANUAL_LIGHT_SOURCE_RATIO",
    "NORMAL_RECENT_TURNS",
    "POST_COMPACTION_MIN_RECENT_TURNS",
    "SUMMARY_SCHEMA_VERSION_DEEP",
    "SUMMARY_SCHEMA_VERSION_LIGHT",
    "choose_retention_decision",
    "count_complete_turns",
    "recent_turn_tail",
    "recent_turns_for_summary_schema",
    "summary_schema_version_for_policy",
    "split_before_recent_turns",
]
