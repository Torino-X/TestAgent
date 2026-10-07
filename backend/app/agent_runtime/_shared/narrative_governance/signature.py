"""Phase 2.9C narrative signature — stable, transport-agnostic hashes.

Signatures are based on business facts only. They must NEVER include
timestamp, model natural language, request id, event public id, or
private user content. Two facts that differ in any of the signature
fields below produce a different signature; everything else is ignored.

The pipeline:

* Caller passes a candidate dict (the public-update payload from
  Phase 2.9A / 2.9B) and a :class:`NarrativeGovernanceContext`.
* ``build_signature`` canonicalises the input, prunes forbidden
  keys, and hashes with ``sha256``. The hash is short (``hashlib
  .hexdigest()[:16]``) for log readability; the full normalized key is
  retained for debugging.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.agent_runtime._shared.narrative_governance.schemas import (
    NarrativeGovernanceContext,
    NarrativeSignature,
)


# Keys that show up often in candidates but must NOT influence the
# signature (they are display-level / transport-level).
_DISPLAY_KEYS: frozenset[str] = frozenset(
    {
        "headline",
        "summary",
        "impact",
        "next_action",
        "nextAction",
        "details",
        "message",
        "title",
        "content",
        "source",
        "timestamp",
        "created_at",
        "updated_at",
        "event_id",
        "public_id",
        "id",
        "sequence_no",
        "request_id",
        "graph_run_id",
        "task_id_human",  # not a real signature key
        "task_internal_id",
        "user_id",
        "user_internal_id",
    }
)


def _canonicalize(value: Any) -> Any:
    """Convert a value into a JSON-stable representation."""
    if isinstance(value, dict):
        items = []
        for key, inner in value.items():
            if key in _DISPLAY_KEYS:
                continue
            items.append((key, _canonicalize(inner)))
        items.sort(key=lambda kv: kv[0])
        return {k: v for k, v in items}
    if isinstance(value, (list, tuple)):
        # Sort lists to make order irrelevant. Nested dicts canonicalize
        # themselves via recursion.
        normalized = [_canonicalize(item) for item in value]
        try:
            normalized.sort(key=_json_sort_key)
        except TypeError:
            # Heterogeneous lists — sort by JSON string for determinism.
            normalized.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=False))
        return normalized
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Fallback: stringify arbitrary objects so the signature stays
    # well-defined for unexpected types.
    return str(value)


def _json_sort_key(value: Any) -> str:
    """Stable sort key that works for dicts and primitives."""
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def _scope_for(context: NarrativeGovernanceContext) -> str:
    """Pick the dedup scope from context (per_task default)."""
    # Phase 2.9C §9.2 — scope is explicit only when the caller
    # specifies it on the context metadata.
    override = context.metadata_scope if hasattr(context, "metadata_scope") else None
    if isinstance(override, str):
        return override
    return "per_task"


def build_signature(
    candidate: dict[str, Any] | None,
    context: NarrativeGovernanceContext,
    *,
    scope: str | None = None,
) -> NarrativeSignature:
    """Build a stable signature for semantic dedup.

    ``candidate`` is the public-update payload from 2.9A/2.9B.
    ``context`` provides the business facts (update_kind, agent_name,
    outcome code, retry strategy, etc).
    """
    payload: dict[str, Any] = {
        "update_kind": context.update_kind or "",
        "agent_name": context.agent_name or "",
        "tool_name": context.tool_name or "",
        "phase": context.phase or "",
        "action": context.action or "",
        "route": context.route or "",
        "outcome_code": context.outcome_code or "",
        "issue_codes": sorted([code for code in (context.issue_codes or [])]),
        "retry_strategy": context.retry_strategy or "",
        "artifact_version": context.artifact_version or "",
        "scope_ids": sorted([sid for sid in (context.scope_ids or [])]),
    }
    # Only a small allow-list of candidate fields influences dedup;
    # this prevents prompt-injected "secret" strings inside
    # ``headline`` / ``details`` lists from leaking into the
    # signature. ``outcome_code`` / ``route`` / ``retry_attempt`` are
    # the business semantics we actually want stable.
    if isinstance(candidate, dict):
        for key in ("outcome_code", "retry_attempt", "issue_codes"):
            value = candidate.get(key)
            if value is not None and key not in payload:
                payload[f"candidate.{key}"] = value

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=False, default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    effective_scope = scope or "per_task"
    return NarrativeSignature(
        version="v1",
        scope=effective_scope,
        normalized_key=canonical,
        hash=digest[:16],
    )


__all__ = ["build_signature"]
# narrative_governance.signature:基于业务事实的稳定签名(禁带时间戳/模型文/ID/隐私);用于去重 + 缓存命中。
