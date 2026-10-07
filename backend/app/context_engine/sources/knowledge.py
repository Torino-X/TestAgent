"""Knowledge Source Adapter（CE-03 真实实现：使用 RetrievalExecutor）。

CE-03 WP-5：KnowledgeSourceAdapter 使用 RetrievalExecutor——
ES lexical + Qdrant dense + MySQL authoritative recheck + Weighted RRF / Reranker。
仅当本 adapter 装配了 executor 且 `CONTEXT_RETRIEVAL_ENABLED` 时执行真实检索；
否则返回确定性空结果 + warning（allowed degradation，不伪造）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from hashlib import sha1
from typing import Any
from datetime import datetime, timezone

from app.context_engine.models.context import ContextItem, ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind, ContextTrust, RerankStrategy, SourceType
from app.context_engine.models.retrieval import RetrievalRequest, RetrievalScopeFilter
from app.context_engine.models.source import ContextWarning, SourceCollectResult
from app.context_engine.sources.registry import ContextSourceAdapterProtocol
from app.context_engine.sources._helpers import user_internal_id

logger = logging.getLogger(__name__)

_MAX_RETRIEVAL_QUERY_CHARS = 2000
_RERANK_CANDIDATE_MULTIPLIER = 3
_MAX_RERANK_CANDIDATES = 50


@dataclass(frozen=True)
class RetrievalExecutionPolicy:
    enabled: bool
    lexical: bool
    dense: bool
    hybrid: bool
    rerank: bool


def _retrieval_execution_policy(runtime_context: Any) -> RetrievalExecutionPolicy:
    """Resolve task-frozen retrieval semantics, or process flags for Chat."""
    resolver = getattr(runtime_context, "task_flag_resolver", None)
    if resolver is not None:
        try:
            enabled = bool(
                resolver.evaluate("CONTEXT_ENGINE_ENABLED")
                and resolver.evaluate("CONTEXT_RETRIEVAL_ENABLED")
            )
            lexical = enabled and bool(resolver.evaluate("CONTEXT_LEXICAL_RETRIEVAL_ENABLED"))
            dense = enabled and bool(resolver.evaluate("CONTEXT_DENSE_RETRIEVAL_ENABLED"))
            return RetrievalExecutionPolicy(
                enabled=enabled,
                lexical=lexical,
                dense=dense,
                hybrid=bool(
                    lexical
                    and dense
                    and resolver.evaluate("CONTEXT_HYBRID_FUSION_ENABLED")
                ),
                rerank=bool(enabled and resolver.evaluate("CONTEXT_RERANK_ENABLED")),
            )
        except Exception:  # noqa: BLE001 - task authority fails closed
            return RetrievalExecutionPolicy(False, False, False, False, False)

    from app.context_engine.feature_flags import get_context_engine_flags

    flags = get_context_engine_flags()
    enabled = bool(flags.retrieval_implies_engine)
    lexical = bool(enabled and flags.context_lexical_retrieval_enabled)
    dense = bool(enabled and flags.context_dense_retrieval_enabled)
    return RetrievalExecutionPolicy(
        enabled=enabled,
        lexical=lexical,
        dense=dense,
        hybrid=bool(lexical and dense and flags.context_hybrid_fusion_enabled),
        rerank=bool(enabled and flags.context_rerank_enabled),
    )


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _safe_retrieval_query(query_text: str) -> str:
    if len(query_text) <= _MAX_RETRIEVAL_QUERY_CHARS:
        return query_text
    logger.warning(
        "FALLBACK_USED | component=context_engine.knowledge_source | "
        "from=current_user_message | to=truncated_retrieval_query | "
        "reason=query_too_long | original_chars=%d | max_chars=%d",
        len(query_text),
        _MAX_RETRIEVAL_QUERY_CHARS,
    )
    return query_text[:_MAX_RETRIEVAL_QUERY_CHARS]


def _candidate_limit_for_rerank(*, final_limit: int, rerank_enabled: bool) -> int:
    """Keep the chat context compact while giving the reranker enough choices."""
    if not rerank_enabled:
        return final_limit
    return min(
        _MAX_RERANK_CANDIDATES,
        max(final_limit, final_limit * _RERANK_CANDIDATE_MULTIPLIER),
    )


def _company_rag_items_from_runtime(runtime_context: Any) -> tuple[list[ContextItem], list[ContextWarning], bool]:
    """Normalize KnowledgeSearchTool output into CE knowledge items.

    This keeps optional Company RAG guidance on the Context Engine path:
    source collect/select/preflight -> ContextComposer -> LLM invocation.
    """
    payload = getattr(runtime_context, "knowledge_search_result", None)
    if not isinstance(payload, dict) or not payload:
        return [], [], False
    if payload.get("source") != "company_rag":
        return [], [], False

    rendered = _render_company_rag_payload(payload)
    if not rendered:
        return [], [], False

    degraded = bool(payload.get("degraded") or payload.get("disabled_by_config"))
    digest = sha1(rendered.encode("utf-8", errors="ignore")).hexdigest()[:12]
    item = ContextItem(
        item_id=f"knowledge:company-rag:{digest}",
        kind=ContextKind.KNOWLEDGE,
        source_type=SourceType.KNOWLEDGE,
        source_ref="company_rag_search_result",
        title="Company RAG search result",
        content=rendered,
        authority=55 if not degraded else 35,
        relevance_score=0.85 if not degraded else 0.25,
        rerank_score=None,
        priority=0,
        estimated_tokens=max(1, len(rendered) // 3),
        trust=ContextTrust.UNTRUSTED_REFERENCE,
        metadata={
            "source": "company_rag",
            "query": payload.get("query"),
            "hit_count": payload.get("hit_count"),
            "degraded": degraded,
            "error_code": payload.get("error_code"),
            "skip_reason": payload.get("skip_reason"),
        },
    )
    warnings: list[ContextWarning] = []
    if degraded:
        warnings.append(
            ContextWarning(
                code="context.source.company_rag_degraded",
                detail=str(
                    payload.get("error_message")
                    or payload.get("skip_reason")
                    or "Company RAG search degraded."
                )[:500],
                adapter_key="knowledge",
                source_kind=ContextKind.KNOWLEDGE.value,
            )
        )
    logger.info(
        "COMPANY_RAG_CONTEXT_ITEM_ADDED | query_len=%d | hit_count=%s | degraded=%s | item_tokens=%d",
        len(str(payload.get("query") or "")),
        payload.get("hit_count"),
        degraded,
        item.estimated_tokens,
    )
    return [item], warnings, degraded


def _render_company_rag_payload(payload: dict[str, Any]) -> str:
    query = str(payload.get("query") or "").strip()
    hit_count = payload.get("hit_count", 0)
    degraded = bool(payload.get("degraded") or payload.get("disabled_by_config"))
    status = "degraded" if degraded else "completed"
    lines = [
        "Company knowledge base retrieval result:",
        f"- query: {query or '(empty)'}",
        f"- status: {status}",
        f"- hit_count: {hit_count}",
    ]
    if degraded:
        reason = payload.get("error_message") or payload.get("skip_reason") or payload.get("error_code")
        if reason:
            lines.append(f"- degraded_reason: {reason}")

    standards = payload.get("standards")
    if isinstance(standards, list) and standards:
        lines.append("- source_documents: " + ", ".join(str(x) for x in standards[:8]))

    snippets: list[str] = []
    for key in ("similar_projects", "hits", "results"):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("doc_name") or item.get("title") or "").strip()
            snippet = str(item.get("snippet") or item.get("content") or item.get("text") or "").strip()
            if not snippet:
                continue
            prefix = f"{name}: " if name else ""
            snippets.append((prefix + snippet).replace("\r", " ").replace("\n", " ")[:500])
            if len(snippets) >= 8:
                break
        if len(snippets) >= 8:
            break
    if snippets:
        lines.append("- snippets:")
        for idx, snippet in enumerate(snippets, start=1):
            lines.append(f"  {idx}. {snippet}")

    return "\n".join(lines)


class KnowledgeSourceAdapter:
    """知识库来源：CE-03 接真实检索（RetrievalExecutor）。"""

    source_kind = ContextKind.KNOWLEDGE

    def __init__(
        self,
        *,
        executor=None,
        retrieval_enabled: bool = False,
        enforce_runtime_flags: bool = False,
        max_chars: int = 4000,
        top_k: int = 5,
    ) -> None:
        self._executor = executor
        self._retrieval_enabled = retrieval_enabled
        self._enforce_runtime_flags = enforce_runtime_flags
        self._max_chars = max_chars
        self._top_k = top_k

    async def collect(
        self,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        *,
        runtime_context,
        retrieval_requests: list[RetrievalRequest] | None = None,
    ) -> SourceCollectResult:
        started = time.monotonic()

        logger.info(
            "DIAG | KnowledgeSourceAdapter.collect 进入 | "
            "executor=%s | retrieval_enabled=%s | "
            "query_text_len=%d | top_k=%d | "
            "max_chars=%d | "
            "user_id=%s | conversation_id=%s | workspace_key=%s | agent_type=%s",
            self._executor is not None,
            self._retrieval_enabled,
            len(request.retrieval_query or request.current_user_message or ""),
            self._top_k,
            self._max_chars,
            getattr(request, "user_id", "?"),
            getattr(request, "conversation_id", "?"),
            getattr(scope, "workspace_key", "?"),
            getattr(request, "agent_type", "?"),
        )

        company_rag_items, company_rag_warnings, company_rag_degraded = _company_rag_items_from_runtime(
            runtime_context
        )

        execution_policy = (
            _retrieval_execution_policy(runtime_context)
            if self._enforce_runtime_flags
            else RetrievalExecutionPolicy(True, True, True, True, True)
        )
        if self._executor is None or not self._retrieval_enabled or not execution_policy.enabled:
            logger.warning(
                "DIAG | KnowledgeSourceAdapter 早退(executor/enabled 缺失) | "
                "executor=%s | retrieval_enabled=%s",
                self._executor is not None,
                self._retrieval_enabled,
            )
            if company_rag_items:
                return SourceCollectResult(
                    adapter_key="knowledge",
                    kind=self.source_kind,
                    items=company_rag_items,
                    warnings=company_rag_warnings,
                    attempted=True,
                    degraded=company_rag_degraded,
                    failure_code=(
                        "context.source.company_rag_degraded"
                        if company_rag_degraded
                        else None
                    ),
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            return SourceCollectResult(
                adapter_key="knowledge",
                kind=self.source_kind,
                items=[],
                warnings=[
                    ContextWarning(
                        code="context.source.knowledge_not_enabled",
                        detail="Knowledge 检索未启用（CONTEXT_RETRIEVAL_ENABLED 关或无 executor）",
                        adapter_key="knowledge",
                        source_kind=ContextKind.KNOWLEDGE.value,
                    )
                ],
                attempted=True,
                degraded=True,
                failure_code="context.source.knowledge_not_enabled",
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        try:
            async with runtime_context.session_factory() as session:
                internal_uid = user_internal_id(runtime_context, request)
                reqs = retrieval_requests or []
                if not reqs:
                    query_text = _safe_retrieval_query(
                        request.retrieval_query or request.current_user_message or ""
                    )
                    candidate_limit = _candidate_limit_for_rerank(
                        final_limit=self._top_k,
                        rerank_enabled=execution_policy.rerank,
                    )
                    reqs = [
                        RetrievalRequest(
                            query_text=query_text,
                            strategy="on_demand",  # type: ignore[arg-type]
                            source_family="knowledge",
                            top_k=self._top_k,
                            rerank_strategy="weighted_rrf",  # type: ignore[arg-type]
                            scope=RetrievalScopeFilter(
                                mode="union",
                                user_id=internal_uid,
                                workspace_key=scope.workspace_key,
                                agent_type=request.agent_type,
                            ),
                            max_candidates=candidate_limit,
                            requested_final_limit=self._top_k,
                        )
                    ]

                items: list[ContextItem] = list(company_rag_items)
                run_ids: list[str] = []
                warnings: list[ContextWarning] = list(company_rag_warnings)
                degraded = company_rag_degraded

                for rq in reqs:
                    if rq.source_family and rq.source_family != "knowledge":
                        continue
                    updates: dict[str, Any] = {}
                    if execution_policy.rerank:
                        updates["rerank_strategy"] = RerankStrategy.RERANKER
                        if rq.max_candidates is None:
                            updates["max_candidates"] = _candidate_limit_for_rerank(
                                final_limit=rq.top_k,
                                rerank_enabled=True,
                            )
                        if rq.requested_final_limit is None:
                            updates["requested_final_limit"] = rq.top_k
                    effective_request = rq.model_copy(update=updates) if updates else rq
                    fused, run_id = await self._executor.execute(
                        request=effective_request,
                        session=session,
                        user_internal_id=internal_uid,
                        workspace_key=scope.workspace_key,
                        agent_type=request.agent_type,
                        lexical_allowed=execution_policy.lexical,
                        vector_allowed=execution_policy.dense,
                        hybrid_allowed=execution_policy.hybrid,
                        rerank_allowed=execution_policy.rerank,
                    )
                    if run_id:
                        run_ids.append(run_id)
                    if not fused:
                        degraded = True
                        continue
                    for rank, m in enumerate(fused, start=1):
                        items.append(
                            ContextItem(
                                item_id=f"knowledge:{m.chunk_public_id}",
                                kind=ContextKind.KNOWLEDGE,
                                source_type=SourceType.KNOWLEDGE,
                                source_ref=m.chunk_public_id,
                                content=(m.content_excerpt or "")[: self._max_chars],
                                authority=60,
                                relevance_score=m.normalized_score,
                                rerank_score=m.rerank_score,
                                priority=0,
                                estimated_tokens=max(1, len((m.content_excerpt or "")) // 3),
                                trust=ContextTrust.UNTRUSTED_REFERENCE,
                                metadata={
                                    "channel": m.channel,
                                    "document_public_id": m.document_public_id,
                                    "final_rank": m.final_rank,
                                },
                            )
                        )

                if degraded and not items:
                    warnings.append(
                        ContextWarning(
                            code="context.source.knowledge_no_hits",
                            detail="Knowledge 检索无命中，已降级",
                            adapter_key="knowledge",
                            source_kind=ContextKind.KNOWLEDGE.value,
                        )
                    )

                # ── DIAG: Knowledge 最终结果 ──
                logger.info(
                    "DIAG | KnowledgeSourceAdapter.collect 完成 | "
                    "items=%d | run_ids=%d | degraded=%s | "
                    "query_count=%d | warnings=%d",
                    len(items), len(run_ids), degraded, len(reqs), len(warnings),
                )

                # Retrieval runs/candidates are durable observability records;
                # AsyncSession context exit alone does not commit them.
                await session.commit()

                return SourceCollectResult(
                    adapter_key="knowledge",
                    kind=self.source_kind,
                    items=items,
                    warnings=warnings,
                    retrieval_run_ids=run_ids,
                    attempted=True,
                    degraded=degraded,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Knowledge collect failed: %s | detail=%s",
                type(exc).__name__, str(exc)[:200],
            )
            return SourceCollectResult(
                adapter_key="knowledge",
                kind=self.source_kind,
                items=[],
                warnings=[
                    ContextWarning(
                        code="context.source.knowledge_error",
                        detail="知识检索失败，已降级",
                        adapter_key="knowledge",
                        source_kind=ContextKind.KNOWLEDGE.value,
                    )
                ],
                attempted=True,
                degraded=True,
                failure_code="context.source.knowledge_error",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
# auto-appended module-level note: Knowledge Base source adapter。
