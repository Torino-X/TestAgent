"""Stage 5 HA contracts: truthful telemetry, state propagation, and worker health."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge
from app.agent_runtime.observability.metrics_service import _p95
from app.context_engine.indexing.index_worker import IndexWorker
from app.context_engine.maintenance.retention_worker import RetentionWorker
from app.context_engine.models.compose import ContextComposeResult
from app.context_engine.models.context import ContextItem
from app.context_engine.models.enums import ContextKind, SourceType
from app.context_engine.models.selection import DroppedContextRef, SelectedContextSet
from app.context_engine.snapshot.context_state_ref import build_context_state_ref


def _run(coro):
    return asyncio.run(coro)


def _result():
    item = ContextItem(
        item_id="evidence_1",
        kind=ContextKind.EVIDENCE,
        source_type=SourceType.ARTIFACT,
        content="safe evidence",
        authority=50,
        estimated_tokens=12,
        source_ref="artifact_1",
    )
    selected = SelectedContextSet(
        included=[item],
        dropped=[DroppedContextRef(item_id="old", reason="source_quota")],
        section_stats={},
        total_estimated_tokens=12,
    )
    return ContextComposeResult(
        selected=selected,
        estimated_input_tokens=12,
        retrieval_run_ids=["retrieval_1", "retrieval_2"],
        degraded=True,
        snapshot_public_id="snapshot_1",
    )


def test_context_state_ref_uses_actual_selection_and_retrieval_stats():
    state_ref = build_context_state_ref(_result(), profile_key="chat.reply")

    assert state_ref.stats.included_ref_count == 1
    assert state_ref.stats.dropped_ref_count == 1
    assert state_ref.stats.retrieval_run_count == 2
    assert state_ref.stats.degraded is True
    assert state_ref.source_refs[0].source_ref == "artifact_1"


def test_bridge_prefers_invoker_state_ref_and_preserves_real_stats():
    state_ref = build_context_state_ref(_result(), profile_key="chat.reply")

    class _Invoker:
        async def invoke(self, **_kwargs):
            return SimpleNamespace(
                value="ok",
                snapshot_public_id="snapshot_1",
                context_state_ref=state_ref,
                attempts=(object(),),
            )

    result = _run(
        ContextInvokerBridge(invoker=_Invoker(), enabled=True).generate(
            user_id=1,
            call_site="chat.reply",
            llm_task_profile=object(),
            runtime_context=SimpleNamespace(),
        )
    )

    assert result.context_state_patch["context_state"]["stats"]["retrieval_run_count"] == 2
    assert result.stats["included_ref_count"] == 1
    assert result.stats["degraded"] is True


def test_worker_health_is_explicit_before_start_and_after_start_stop():
    worker = IndexWorker(session_factory=lambda: None)
    assert worker.health_snapshot()["running"] is False

    async def _exercise():
        await worker.start()
        await asyncio.sleep(0)
        running = worker.health_snapshot()
        await worker.stop()
        return running, worker.health_snapshot()

    running, stopped = _run(_exercise())
    assert running["running"] is True
    assert running["started_at"] is not None
    assert stopped["running"] is False


def test_index_worker_fault_is_visible_in_heartbeat():
    worker = IndexWorker(session_factory=lambda: None, poll_interval_seconds=0.001)

    async def _broken_poll():
        raise RuntimeError("injected")

    worker._poll_once = _broken_poll

    async def _exercise():
        await worker.start()
        await asyncio.sleep(0.01)
        snapshot = worker.health_snapshot()
        await worker.stop()
        return snapshot

    snapshot = _run(_exercise())
    assert snapshot["poll_errors_total"] >= 1
    assert snapshot["last_error_code"] == "context.index.poll.RuntimeError"


def test_retention_health_exposes_mode_without_sensitive_configuration():
    worker = RetentionWorker(session_factory=lambda: None, dry_run=True)
    snapshot = worker.health_snapshot()

    assert snapshot["running"] is False
    assert snapshot["dry_run"] is True
    assert "lock_name" not in snapshot


def test_latency_percentile_is_bounded_and_empty_safe():
    assert _p95([]) is None
    assert _p95([1, 100, 2, 3, 4]) == 4
