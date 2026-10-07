"""ContextStateRef 构建：由 ComposeResult 构建轻量 State 引用。

CE-02 WP-7：≤20KB，source_refs≤100，含 latest_snapshot_id + stats + source
refs + to_state_dict。
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
    """由 ContextComposeResult 构建 ContextStateRef。"""
    selected: SelectedContextSet | None = result.selected
    included = selected.included if selected is not None else []
    dropped = selected.dropped if selected is not None else []
    refs = [
        ContextRef(
            item_id=it.item_id,
            kind=it.kind,
            source_type=it.source_type,
            source_ref=it.source_ref,
        )
        for it in included
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
# auto-appended module-level note: context state ref: 跨进程 ContextEngine state 句柄。
