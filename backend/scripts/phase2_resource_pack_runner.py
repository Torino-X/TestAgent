"""Standalone, resource-pack-driven Phase 2 LCT scenarios.

This is intentionally a small executable test harness, not a pytest launcher.
It imports production Context Engine code and builds disposable SQLite/fake
dependencies locally so each LCT runs in isolation and leaves a diagnostic
result bundle under ``test-results/phase2-resource-pack``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

# When launched as ``python scripts/phase2_resource_pack_runner.py``, Python
# adds ``scripts`` but not its ``backend`` parent to sys.path.  Production code
# lives at backend/app, so make that explicit without depending on caller CWD.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from phase2_lct_runtime import LctRun, cli_failure, repository_root


def _run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)


def _item(item_id: str, kind, *, content: str, tokens: int, source_type=None, authority: int = 60):
    from app.context_engine.models.context import ContextItem
    from app.context_engine.models.enums import ContextTrust, SourceType

    return ContextItem(
        item_id=item_id,
        kind=kind,
        source_type=source_type or SourceType.CONVERSATION,
        source_ref=item_id,
        title=item_id,
        content=content,
        authority=authority,
        priority=5,
        estimated_tokens=tokens,
        trust=ContextTrust.BUSINESS_EVIDENCE,
        metadata={},
    )


def _selected(*items):
    from app.context_engine.models.selection import SelectedContextSet

    return SelectedContextSet(
        included=list(items),
        dropped=[],
        section_stats={},
        total_estimated_tokens=sum(item.estimated_tokens for item in items),
        locked_section_ids=[],
    )


def _plan():
    from app.context_engine.models.context import ContextKind, ContextPlan, SectionPlan
    from app.context_engine.models.profile import ContextBudget

    budget = ContextBudget(
        model_context_window=10_000,
        target_input=1_000,
        soft_threshold=7_000,
        hard_compact_threshold=8_500,
        absolute_threshold=9_500,
    )
    return ContextPlan(
        profile_key="phase2.resource_pack",
        profile_version="v1",
        model_context_window=budget.model_context_window,
        input_budget=budget.target_input,
        output_reserve=budget.output_reserve,
        runtime_reserve=budget.runtime_reserve,
        safety_margin=budget.safety_margin,
        section_plans={
            "memory": SectionPlan(kind=ContextKind.MEMORY, required=False, budget_tokens=1_000)
        },
        compression_policy="conversation_compaction",
    )


class _PackCompactor:
    """Local deterministic compaction provider; it is not a product LLM call."""

    def __init__(self, summary_text: str, tokens_after: int = 240):
        from app.context_engine.models.enums import CompactionType

        self.compaction_type = CompactionType.CONVERSATION
        self.summary_text = summary_text
        self.tokens_after = tokens_after
        self.calls = 0
        self.request = None

    async def compact(self, request, *, runtime_context=None):
        from app.context_engine.compression.models import ContextCompactionResult

        self.calls += 1
        self.request = request
        return ContextCompactionResult(
            run_public_id="phase2-local-compaction-run",
            summary_public_id="phase2-local-summary-v1",
            summary_text=self.summary_text,
            summary_type="conversation",
            tokens_after=self.tokens_after,
            compression_ratio=self.tokens_after / request.tokens_before,
        )


def _compaction_fixture(*, old_marker: str, new_marker: str):
    from app.context_engine.models.context import ContextKind, ContextRequest
    from app.context_engine.models.enums import SourceType

    # System/current goal are real protected kinds.  User-defined marker text is
    # deliberately not treated as an anchor by this harness.
    system = _item(
        "system-rules", ContextKind.SYSTEM_RULES,
        content="SYSTEM_ANCHOR: safety and response contract", tokens=100, source_type=SourceType.SYSTEM,
    )
    goal = _item(
        "current-goal", ContextKind.CURRENT_GOAL,
        content="CURRENT_GOAL_ANCHOR: finish Phase 2 lifecycle verification", tokens=100,
    )
    turns = [
        _item(
            f"turn-{number}", ContextKind.CONVERSATION,
            content=f"{old_marker} historical turn {number}; {new_marker if number == 4 else ''}",
            tokens=2_500,
        )
        for number in range(1, 5)
    ]
    summary = (
        "COMPACTION_SUMMARY: historical discussion compacted; "
        f"{new_marker} remains the current factual outcome; open task is Phase 2 verification."
    )
    return (
        ContextRequest(user_id="1", call_site="phase2.lct", current_user_message="continue from current state"),
        _selected(system, goal, *turns),
        _PackCompactor(summary),
        old_marker,
        new_marker,
    )


async def _new_sqlite_factory():
    """Create a fresh in-memory database; no application database is touched."""
    import app.models  # noqa: F401 - registers all models on Base
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool
    from app.db.base import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_candidate(sf, *, content: str, workspace_key: str | None, evidence: str, scope_type: str = "workspace"):
    from app.context_engine.memory import MemoryService

    async with sf() as session:
        result = await MemoryService(session).create_candidate(
            user_id=1,
            scope_type=scope_type,
            workspace_key=workspace_key,
            agent_type=None,
            memory_type="fact",
            content=content,
            evidence=[{"source_public_id": "phase2-source", "evidence_excerpt": evidence}],
        )
        await session.commit()
        return result


async def _activate(sf, public_id: str):
    from app.context_engine.memory import MemoryService

    async with sf() as session:
        result = await MemoryService(session).activate(user_id=1, memory_public_id=public_id)
        await session.commit()
        return result


class _MemoryRuntime:
    def __init__(self, sf):
        self._sf = sf
        self.user_internal_id = 1
        self.task_flag_resolver = SimpleNamespace(evaluate=lambda name: name == "CONTEXT_MEMORY_READ_ENABLED")

    def session_factory(self):
        return self._sf()


def lct_01(run: LctRun) -> str:
    """LCT-01: threshold compaction, protected built-in anchors, replacement."""
    from app.context_engine.composer.composer import ContextComposer
    from app.context_engine.compression.preflight_service import ContextPreflightService
    from app.context_engine.models.enums import CompactionStatus

    request, selected, compactor, old_marker, new_marker = _compaction_fixture(
        old_marker="LCT01_OLD_HISTORY_MUST_BE_REPLACED", new_marker="LCT01_NEW_FACT"
    )
    run.log("LCT-01: execute hard/absolute preflight compaction with local provider")
    result = _run(
        ContextPreflightService(enabled=True, conversation_compactor=compactor).run(
            request=request, plan=_plan(), selected=selected, runtime_context=SimpleNamespace(user_internal_id=1)
        )
    )
    prompt = ContextComposer().compose(request, result.selected).prompt_text
    evidence_path = run.write_evidence("compaction-result.json", {
        "tokens_before": result.tokens_before, "tokens_after": result.tokens_after,
        "status": result.status.value, "summary_refs": [str(ref) for ref in result.compacted_summary_refs],
        "provider_source_payload": compactor.request.source_payload,
        "prompt": prompt,
    })
    step = run.step("threshold triggers actual preflight compaction", component="ContextPreflightService", expected="COMPACTED with a provider call")
    run.check(step, result.status == CompactionStatus.COMPACTED and compactor.calls == 1,
              actual=f"status={result.status.value}, provider_calls={compactor.calls}", evidence={"result": evidence_path})
    step = run.step("summary replaces compactable history", component="ContextPreflightService + ContextComposer", expected="token decrease and no raw old history in composed prompt")
    run.check(step, result.tokens_after < result.tokens_before and old_marker not in prompt and "COMPACTION_SUMMARY" in prompt,
              actual=f"before={result.tokens_before}, after={result.tokens_after}, old_in_prompt={old_marker in prompt}", evidence={"result": evidence_path})
    step = run.step("built-in anchors remain", component="ContextPreflightService", expected="SYSTEM_RULES and CURRENT_GOAL remain after compaction")
    run.check(step, "SYSTEM_ANCHOR" in prompt and "CURRENT_GOAL_ANCHOR" in prompt and new_marker in prompt,
              actual=f"system_anchor={'SYSTEM_ANCHOR' in prompt}, goal_anchor={'CURRENT_GOAL_ANCHOR' in prompt}, new_fact={new_marker in prompt}", evidence={"result": evidence_path})
    run.notes.append("AUTOMATED_ONLY: local deterministic compactor verifies the state contract; it does not prove a live API/audit UI path.")
    return "AUTOMATED_ONLY"


def lct_02(run: LctRun) -> str:
    """LCT-02: no raw-history rehydration in the selected/composed context."""
    from app.context_engine.composer.composer import ContextComposer
    from app.context_engine.compression.preflight_service import ContextPreflightService
    from app.context_engine.models.enums import CompactionStatus

    request, selected, compactor, old_marker, new_marker = _compaction_fixture(
        old_marker="LCT02_SUPERSEDED_VALUE", new_marker="LCT02_CURRENT_VALUE"
    )
    run.log("LCT-02: compact selected history and inspect the post-preflight composition")
    result = _run(
        ContextPreflightService(enabled=True, conversation_compactor=compactor).run(
            request=request, plan=_plan(), selected=selected, runtime_context=SimpleNamespace(user_internal_id=1)
        )
    )
    prompt = ContextComposer().compose(request, result.selected).prompt_text
    included = [{"id": item.item_id, "content": item.content, "source_type": str(item.source_type)} for item in result.selected.included]
    evidence_path = run.write_evidence("anti-rehydration-result.json", {
        "preflight_status": result.status.value, "included": included, "dropped": [str(d) for d in result.selected.dropped], "prompt": prompt,
    })
    step = run.step("compaction state transition", component="ContextPreflightService", expected="COMPACTED with compacted source items marked superseded")
    run.check(step, result.status == CompactionStatus.COMPACTED and len(result.selected.dropped) >= 4,
              actual=f"status={result.status.value}, dropped={len(result.selected.dropped)}", evidence={"result": evidence_path})
    step = run.step("no old selected history reaches composition", component="ContextComposer", expected="old value absent; current summary fact present")
    run.check(step, old_marker not in prompt and new_marker in prompt,
              actual=f"old_in_prompt={old_marker in prompt}, current_in_prompt={new_marker in prompt}", evidence={"result": evidence_path})
    step = run.step("raw-record retention is not misreported as deletion", component="scenario oracle", expected="test only claims replacement in selected/composed context")
    run.check(step, True, actual="raw source is not a deletion target in this scenario", evidence={"result": evidence_path})
    run.notes.append("AUTOMATED_ONLY: validates preflight replacement, not universal deletion of raw conversation history or a live rehydration endpoint.")
    return "AUTOMATED_ONLY"


async def _lct_03_async(run: LctRun):
    from sqlalchemy import select
    from app.context_engine.memory import MemoryService
    from app.context_engine.models.context import ContextKind, ContextRequest, ContextScope, SectionPlan
    from app.context_engine.sources.memory import MemorySourceAdapter
    from app.models.context_engine import ContextMemory, ContextMemorySource

    engine, sf = await _new_sqlite_factory()
    try:
        first = await _seed_candidate(sf, content="LCT03 original preference", workspace_key="project:lct03", evidence="original evidence")
        await _activate(sf, first["memory_public_id"])
        replacement = await _seed_candidate(sf, content="LCT03 updated preference", workspace_key="project:lct03", evidence="replacement evidence")
        await _activate(sf, replacement["memory_public_id"])
        async with sf() as session:
            forgotten = await MemoryService(session).forget(user_id=1, memory_public_id=first["memory_public_id"])
            await session.commit()
        async with sf() as session:
            rows = (await session.execute(select(ContextMemory).order_by(ContextMemory.id))).scalars().all()
            sources = (await session.execute(select(ContextMemorySource))).scalars().all()
        retrieved = await MemorySourceAdapter(top_k=10).collect(
            ContextRequest(user_id="usr_1", call_site="phase2.lct03"),
            SectionPlan(kind=ContextKind.MEMORY, required=False, budget_tokens=1000),
            ContextScope(user_id="usr_1", thread_id="lct03", workspace_key="project:lct03"),
            runtime_context=_MemoryRuntime(sf),
        )
        return first, replacement, forgotten, rows, sources, retrieved
    finally:
        await engine.dispose()


def lct_03(run: LctRun) -> str:
    run.log("LCT-03: exercise candidate → active → forget lifecycle in disposable SQLite")
    first, replacement, forgotten, rows, sources, retrieved = _run(_lct_03_async(run))
    evidence_path = run.write_evidence("memory-lifecycle.json", {
        "first": first, "replacement": replacement, "forgotten": forgotten,
        "rows": [{"public_id": row.public_id, "status": row.status, "content": row.content, "deleted_at": str(row.deleted_at), "supersedes_memory_id": row.supersedes_memory_id} for row in rows],
        "source_evidence": [{"memory_id": source.memory_id, "evidence_excerpt": source.evidence_excerpt} for source in sources],
        "retrieved_source_refs": [item.source_ref for item in retrieved.items],
    })
    original = next(row for row in rows if row.public_id == first["memory_public_id"])
    replacement_row = next(row for row in rows if row.public_id == replacement["memory_public_id"])
    step = run.step("memory lifecycle transitions", component="MemoryService", expected="candidate → active and active → forgotten")
    run.check(step, first["status"] == "candidate" and forgotten["status"] == "forgotten" and original.status == "forgotten",
              actual=f"created={first['status']}, forgotten={forgotten['status']}, stored={original.status}", evidence={"result": evidence_path})
    step = run.step("forget clears recoverable content and evidence", component="MemoryService", expected="forgotten row content/evidence cleared and tombstoned")
    original_sources = [source for source in sources if source.memory_id == original.id]
    run.check(step, original.content == "" and original.deleted_at is not None and all(source.evidence_excerpt == "" for source in original_sources),
              actual=f"content={original.content!r}, deleted_at={original.deleted_at is not None}, sources_cleared={all(source.evidence_excerpt == '' for source in original_sources)}", evidence={"result": evidence_path})
    step = run.step("only active memory composes", component="MemorySourceAdapter", expected="replacement active memory is returned; forgotten original is absent")
    returned = {item.source_ref for item in retrieved.items}
    run.check(step, replacement["memory_public_id"] in returned and first["memory_public_id"] not in returned and replacement_row.status == "active",
              actual=f"returned={sorted(returned)}, replacement_status={replacement_row.status}", evidence={"result": evidence_path})
    run.notes.append("AUTOMATED_ONLY: validates lifecycle/storage composition contract with a fresh SQLite database; API and UI evidence remain separate acceptance work.")
    return "AUTOMATED_ONLY"


async def _lct_04_async():
    from app.context_engine.models.context import ContextKind, ContextRequest, ContextScope, SectionPlan
    from app.context_engine.scope import ContextScopeResolver
    from app.context_engine.sources.memory import MemorySourceAdapter

    engine, sf = await _new_sqlite_factory()
    try:
        a = await _seed_candidate(sf, content="LCT04 workspace A private fact", workspace_key="project:alpha", evidence="A evidence")
        b = await _seed_candidate(sf, content="LCT04 workspace B private fact", workspace_key="project:beta", evidence="B evidence")
        await _activate(sf, a["memory_public_id"])
        await _activate(sf, b["memory_public_id"])
        adapter = MemorySourceAdapter(top_k=10)
        section = SectionPlan(kind=ContextKind.MEMORY, required=False, budget_tokens=1000)
        request = ContextRequest(user_id="usr_1", call_site="phase2.lct04")
        alpha = await adapter.collect(request, section, ContextScope(user_id="usr_1", thread_id="alpha", workspace_key="project:alpha"), runtime_context=_MemoryRuntime(sf))
        beta = await adapter.collect(request, section, ContextScope(user_id="usr_1", thread_id="beta", workspace_key="project:beta"), runtime_context=_MemoryRuntime(sf))
        resolver = ContextScopeResolver()
        frozen = resolver.resolve_workspace(user_id="usr_1", task_workspace="project:alpha", conversation_workspace="project:beta")
        return a, b, alpha, beta, frozen
    finally:
        await engine.dispose()


def lct_04(run: LctRun) -> str:
    run.log("LCT-04: create two workspace memories and query each scope independently")
    a, b, alpha, beta, frozen = _run(_lct_04_async())
    alpha_ids = {item.source_ref for item in alpha.items}
    beta_ids = {item.source_ref for item in beta.items}
    evidence_path = run.write_evidence("scope-isolation.json", {
        "workspace_alpha": sorted(alpha_ids), "workspace_beta": sorted(beta_ids),
        "frozen_workspace_resolution": {"workspace_key": frozen.workspace_key, "source": frozen.source},
    })
    step = run.step("workspace A isolation", component="MemorySourceAdapter", expected="only workspace A active memory is returned")
    run.check(step, a["memory_public_id"] in alpha_ids and b["memory_public_id"] not in alpha_ids,
              actual=f"alpha_ids={sorted(alpha_ids)}", evidence={"result": evidence_path})
    step = run.step("workspace B isolation", component="MemorySourceAdapter", expected="only workspace B active memory is returned")
    run.check(step, b["memory_public_id"] in beta_ids and a["memory_public_id"] not in beta_ids,
              actual=f"beta_ids={sorted(beta_ids)}", evidence={"result": evidence_path})
    step = run.step("frozen task workspace wins", component="ContextScopeResolver", expected="task workspace remains alpha despite conversation beta")
    run.check(step, frozen.workspace_key == "project:alpha" and frozen.source == "task_frozen",
              actual=f"workspace={frozen.workspace_key}, source={frozen.source}", evidence={"result": evidence_path})
    run.notes.append("AUTOMATED_ONLY: verifies workspace isolation in the production adapter with local data; it does not substitute for cross-workspace UI/API authorization evidence.")
    return "AUTOMATED_ONLY"


class _AuditExecutor:
    """A real RetrievalExecutor with an in-memory audit sink for LCT fault injection."""

    def __new__(cls, **kwargs):
        from app.context_engine.retrieval.executor import RetrievalExecutor

        class Recorder(RetrievalExecutor):
            def __init__(self, **inner_kwargs):
                super().__init__(**inner_kwargs)
                self.audit_calls: list[dict[str, Any]] = []

            async def _audit(self, **audit_kwargs):
                self.audit_calls.append(audit_kwargs)
                return "phase2-retrieval-audit"

        return Recorder(**kwargs)


class _Lexical:
    enabled = True

    def __init__(self, *, fail: bool = False):
        self.fail = fail

    async def search(self, **kwargs):
        if self.fail:
            raise RuntimeError("injected lexical outage")
        return [_chunk("lexical")]


class _Vector:
    enabled = True

    def __init__(self, *, fail: bool = False):
        self.fail = fail

    async def search(self, **kwargs):
        if self.fail:
            raise RuntimeError("injected vector outage")
        return [_chunk("vector")]


class _Embedding:
    async def embed(self, request):
        return SimpleNamespace(vectors=[SimpleNamespace(values=[0.1, 0.2])])


class _FailingReranker:
    enabled = True

    async def rerank(self, request):
        raise RuntimeError("injected reranker outage")


def _chunk(channel: str):
    from app.context_engine.indexing.stores_protocol import RetrievedChunk

    return RetrievedChunk(chunk_public_id="lct05-chunk", document_public_id="lct05-document", user_id=1, workspace_key=None, score=0.9, channel=channel, rank=1)


def _retrieval_request(*, reranker: bool = False):
    from app.context_engine.models.enums import RerankStrategy
    from app.context_engine.models.retrieval import RetrievalRequest, RetrievalScopeFilter

    return RetrievalRequest(query_text="LCT05 continuity query", source_family="knowledge", top_k=5,
                            rerank_strategy=RerankStrategy.RERANKER if reranker else RerankStrategy.WEIGHTED_RRF,
                            scope=RetrievalScopeFilter(mode="user_global", user_id=1))


async def _approved_recheck(**kwargs):
    from app.context_engine.retrieval.retrieval import RecheckResult

    approved = {"chunk_public_id": "lct05-chunk", "document_public_id": "lct05-document", "document_id": 1, "chunk_id": 1,
                "user_id": 1, "workspace_key": None, "content": "approved local retrieval fact", "content_hash": "phase2",
                "estimated_tokens": 4, "source_public_id": "phase2-file", "source_version": "1"}
    return RecheckResult(approved=[approved], observed_by_chunk={"lct05-chunk": approved})


async def _acl_reject_recheck(**kwargs):
    from app.context_engine.retrieval.retrieval import RecheckResult

    observed = {"chunk_public_id": "lct05-chunk", "drop_reason": "acl_denied"}
    return RecheckResult(approved=[], observed_by_chunk={"lct05-chunk": observed}, rejected_by_chunk={"lct05-chunk": "acl_denied"})


async def _execute_retrieval(executor, *, request, **flags):
    return await executor.execute(request=request, session=object(), user_internal_id=1, workspace_key=None, **flags)


def lct_05(run: LctRun) -> str:
    """LCT-05: every failure is a local injectable dependency, never shared infra."""
    import app.context_engine.retrieval.executor as executor_module

    run.log("LCT-05: inject lexical/vector/reranker/ACL faults into isolated retrieval executor")
    original_recheck = executor_module.mysql_authoritative_recheck
    records: dict[str, Any] = {}
    try:
        executor_module.mysql_authoritative_recheck = _approved_recheck
        d1 = _AuditExecutor(lexical_store=_Lexical(fail=True), lexical_namespace="lct05-lex", vector_store=_Vector(), embedding_provider=_Embedding(), embedding_dimension=2, embedding_model="local", embedding_namespace="lct05-vec")
        records["D1_lexical_failure"] = _run(_execute_retrieval(d1, request=_retrieval_request())) + (d1.audit_calls[-1],)
        d2 = _AuditExecutor(lexical_store=_Lexical(), lexical_namespace="lct05-lex", vector_store=_Vector(fail=True), embedding_provider=_Embedding(), embedding_dimension=2, embedding_model="local", embedding_namespace="lct05-vec")
        records["D2_vector_failure"] = _run(_execute_retrieval(d2, request=_retrieval_request())) + (d2.audit_calls[-1],)
        d3 = _AuditExecutor(lexical_store=_Lexical(), lexical_namespace="lct05-lex", rerank_service=_FailingReranker())
        records["D3_reranker_failure"] = _run(_execute_retrieval(d3, request=_retrieval_request(reranker=True), rerank_allowed=True)) + (d3.audit_calls[-1],)
        d4 = _AuditExecutor(lexical_store=_Lexical(), lexical_namespace="lct05-lex")
        records["D4_all_channels_disabled"] = _run(_execute_retrieval(d4, request=_retrieval_request(), lexical_allowed=False, vector_allowed=False)) + (d4.audit_calls[-1],)
        executor_module.mysql_authoritative_recheck = _acl_reject_recheck
        d5 = _AuditExecutor(lexical_store=_Lexical(), lexical_namespace="lct05-lex")
        records["D5_acl_rejection"] = _run(_execute_retrieval(d5, request=_retrieval_request())) + (d5.audit_calls[-1],)
    finally:
        executor_module.mysql_authoritative_recheck = original_recheck
    def audit(name): return records[name][2]
    evidence_path = run.write_evidence("retrieval-fault-matrix.json", {
        name: {"hits": len(value[0]), "run_id": value[1], "audit": value[2]} for name, value in records.items()
    })
    step = run.step("D1 lexical outage degrades to vector", component="RetrievalExecutor", expected="one approved hit, lexical off, lexical_unavailable")
    run.check(step, len(records["D1_lexical_failure"][0]) == 1 and not audit("D1_lexical_failure")["lexical_on"] and audit("D1_lexical_failure")["vector_on"] and audit("D1_lexical_failure")["fallback_code"] == "context.retrieval.lexical_unavailable",
              actual=str(audit("D1_lexical_failure")), evidence={"matrix": evidence_path})
    step = run.step("D2 vector outage degrades to lexical", component="RetrievalExecutor", expected="one approved hit, vector off, vector_unavailable")
    run.check(step, len(records["D2_vector_failure"][0]) == 1 and audit("D2_vector_failure")["lexical_on"] and not audit("D2_vector_failure")["vector_on"] and audit("D2_vector_failure")["fallback_code"] == "context.retrieval.vector_unavailable",
              actual=str(audit("D2_vector_failure")), evidence={"matrix": evidence_path})
    step = run.step("D3 reranker outage falls back to RRF", component="RetrievalExecutor", expected="one hit, rerank off, reranker_unavailable")
    run.check(step, len(records["D3_reranker_failure"][0]) == 1 and not audit("D3_reranker_failure")["rerank_on"] and audit("D3_reranker_failure")["fallback_code"] == "context.retrieval.reranker_unavailable",
              actual=str(audit("D3_reranker_failure")), evidence={"matrix": evidence_path})
    step = run.step("D4 all channels disabled fails closed", component="RetrievalExecutor", expected="no hit and no_channel_enabled")
    run.check(step, not records["D4_all_channels_disabled"][0] and audit("D4_all_channels_disabled")["fallback_code"] == "context.retrieval.no_channel_enabled",
              actual=str(audit("D4_all_channels_disabled")), evidence={"matrix": evidence_path})
    step = run.step("D5 authoritative ACL rejection removes candidate", component="RetrievalExecutor", expected="no selected hit and acl_filtered")
    run.check(step, not records["D5_acl_rejection"][0] and audit("D5_acl_rejection")["fallback_code"] == "context.retrieval.acl_filtered",
              actual=str(audit("D5_acl_rejection")), evidence={"matrix": evidence_path})
    run.notes.append("AUTOMATED_ONLY: all faults are local stubs; this runner never starts or stops ES, Qdrant, Redis, or shared services.")
    return "AUTOMATED_ONLY"


async def _lct_06_async():
    from sqlalchemy import select
    from app.context_engine.compression.agent_loop_compactor import AgentLoopCompactor
    from app.context_engine.compression.models import ContextCompactionRequest
    from app.context_engine.models.enums import CompactionTriggerType, CompactionType, RecoveryMode
    from app.context_engine.models.tool_output import ToolOutputPolicy
    from app.context_engine.tool_output import ToolOutputManager, TypedToolOutput
    from app.models.conversation_summary import ConversationSummary

    class Invoker:
        async def invoke(self, **kwargs):
            return SimpleNamespace(value="LCT06 loop summary: decision and argument signatures retained")

    class PayloadService:
        async def put(self, command, *, session_factory):
            return SimpleNamespace(payload_public_id="lct06-payload")

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def commit(self): return None

    engine, sf = await _new_sqlite_factory()
    try:
        runtime = SimpleNamespace(session_factory=sf, context_llm_invoker=Invoker())
        compactor = AgentLoopCompactor(session_factory=sf)
        def request(digest):
            return ContextCompactionRequest(request_id="lct06-request", user_id=1, conversation_id=30, task_id=20,
                call_site="phase2.lct06", compaction_type=CompactionType.AGENT_LOOP, trigger=CompactionTriggerType.PREFLIGHT,
                policy_key="compression.agent_loop:v1", policy_version="v1", source_digest=digest, tokens_before=500,
                target_tokens=200, recovery_mode=RecoveryMode.SUMMARY_WITH_REFS, require_recovery_payload=True,
                source_payload={"steps_audit": "decision=repair; args_signature=repair(section_b)"})
        first = await compactor.compact(request("a" * 64), runtime_context=runtime)
        second = await compactor.compact(request("b" * 64), runtime_context=runtime)
        async with sf() as session:
            summaries = (await session.execute(select(ConversationSummary).where(ConversationSummary.summary_type == "agent_loop").order_by(ConversationSummary.id))).scalars().all()
        manager = ToolOutputManager(PayloadService())
        small = await manager.manage(1, "task", "small", raw_output=TypedToolOutput(text="ok"), policy=ToolOutputPolicy())
        medium = await manager.manage(1, "task", "medium", raw_output=TypedToolOutput(text="abcdefghijklmnop"), policy=ToolOutputPolicy(inline_char_limit=10, head_chars=5, tail_chars=3))
        binary = await manager.manage(1, "task", "binary", raw_output=TypedToolOutput(data=b"\x00\x01", media_type="application/octet-stream"), policy=ToolOutputPolicy(), session_factory=lambda: Session())
        return first, second, summaries, small, medium, binary
    finally:
        await engine.dispose()


def lct_06(run: LctRun) -> str:
    run.log("LCT-06: run AgentLoopCompactor and ToolOutputManager internal contracts")
    first, second, summaries, small, medium, binary = _run(_lct_06_async())
    evidence_path = run.write_evidence("agent-loop-tool-output.json", {
        "compaction": {"first_status": first.status.value if first else None, "second_status": second.status.value if second else None,
                       "summaries": [{"type": summary.summary_type, "status": summary.status, "tokens_before": summary.tokens_before, "tokens_after": summary.tokens_after} for summary in summaries]},
        "tool_output": {"small": {"preview": small.preview, "truncated": small.truncated}, "medium": {"mode": medium.truncation_metadata.mode, "truncated": medium.truncated}, "binary": {"mode": binary.truncation_metadata.mode, "payload_ref": binary.payload_ref}},
    })
    step = run.step("agent-loop summary/version chain", component="AgentLoopCompactor", expected="two agent_loop summaries, first superseded and second active")
    run.check(step, first is not None and second is not None and len(summaries) == 2 and summaries[0].status == "superseded" and summaries[1].status == "active",
              actual=f"first={getattr(first, 'status', None)}, second={getattr(second, 'status', None)}, rows={[s.status for s in summaries]}", evidence={"result": evidence_path})
    step = run.step("tool output containment", component="ToolOutputManager", expected="small inline, medium head_tail, binary metadata-only durable ref")
    run.check(step, not small.truncated and medium.truncation_metadata.mode == "head_tail" and binary.truncation_metadata.mode == "metadata_only" and binary.payload_ref == "lct06-payload",
              actual=f"small={small.truncated}, medium={medium.truncation_metadata.mode}, binary={binary.truncation_metadata.mode}/{binary.payload_ref}", evidence={"result": evidence_path})
    run.notes.append("NOT_IMPLEMENTED: production Agent graph automatic AgentLoop compaction trigger is not wired. These are internal-component scenarios only and cannot close LCT-06 as a product flow.")
    return "NOT_IMPLEMENTED"


SCENARIOS: dict[str, Callable[[LctRun], str]] = {
    "LCT-01": lct_01, "LCT-02": lct_02, "LCT-03": lct_03,
    "LCT-04": lct_04, "LCT-05": lct_05, "LCT-06": lct_06,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one standalone Phase 2 resource-pack LCT")
    parser.add_argument("--lct", required=True, choices=sorted(SCENARIOS))
    args = parser.parse_args()
    execution_path = "AUTOMATED_ONLY" if args.lct != "LCT-06" else "AUTOMATED_ONLY + NOT_IMPLEMENTED"
    run = LctRun(lct=args.lct, repo_root=repository_root(), execution_path=execution_path)
    try:
        verdict = SCENARIOS[args.lct](run)
        return run.finish(lct_verdict=verdict, classification="resource-pack standalone scenario")
    except Exception as exc:  # report every failure before preserving the non-zero exit code
        return cli_failure(run, exc, classification="resource-pack standalone scenario")


if __name__ == "__main__":
    raise SystemExit(main())
