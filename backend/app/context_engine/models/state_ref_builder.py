"""Pure builder for the serialized Context Engine state reference.

This is model-boundary code: it only converts compose-model data into a
``ContextStateRef``. Agent Runtime can depend on it without importing the
snapshot implementation layer.
"""

from __future__ import annotations

from app.context_engine.models.compose import ContextComposeResult
from app.context_engine.models.context import ContextRef
from app.context_engine.models.selection import SelectedContextSet
from app.context_engine.models.snapshot_models import ContextStateRef, ContextStateStats


def build_context_state_ref(
    result: ContextComposeResult,
    *,
    snapshot_public_id: str | None = None,
    profile_key: str = "",
) -> ContextStateRef:
    """Build the bounded, serializable state reference from a compose result."""
    selected: SelectedContextSet | None = result.selected
    included = selected.included if selected is not None else []
    dropped = selected.dropped if selected is not None else []
    refs = [
        ContextRef(
            item_id=item.item_id,
            kind=item.kind,
            source_type=item.source_type,
            source_ref=item.source_ref,
        )
        for item in included
    ]
    stats = ContextStateStats(
        included_ref_count=len(included),
        dropped_ref_count=len(dropped),
        retrieval_run_count=len(result.retrieval_run_ids),
        estimated_input_tokens=result.estimated_input_tokens,
        degraded=bool(result.degraded or dropped),
        locked_section_ids=selected.locked_section_ids if selected else [],
    )
    return ContextStateRef(
        latest_snapshot_public_id=snapshot_public_id or (result.snapshot_public_id or ""),
        profile_key=profile_key,
        stats=stats,
        source_refs=refs,
    )


__all__ = ["build_context_state_ref"]
