"""Retrieval Audit Service：统一审计写入口（Knowledge + Memory）。

CE-03 WP-5：
- record_knowledge_run：RetrievalExecutor 用——写 context_retrieval_runs +
  context_retrieval_candidates；
- record_memory_run：MemorySourceAdapter 用——只写统计字段
  （source_family='memory' / scope_type / candidate_count / selected_count /
   latency / drop_reason / fallback_code），**正文与 Evidence 全文不进审计表**；
- 返回 run_public_id，经 SourceCollectResult.retrieval_run_ids →
  ContextSnapshotBeginCommand.retrieval_run_ids → retrieval_run_ids_json。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.context_engine.models.retrieval import RetrievalRequest


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _gen_public_id(prefix: str) -> str:
    import uuid

    return prefix + uuid.uuid4().hex[:40]


class RetrievalAuditService:
    """检索审计：每 query 一条 run + 每候选一条 candidate。

    Memory 候选无 index_chunk 引用 → 写 memory 统计字段（不含正文）。
    """

    def __init__(self, session) -> None:
        self._session = session

    async def record_knowledge_run(
        self,
        *,
        user_id: int,
        workspace_key: str | None,
        request: RetrievalRequest,
        lexical_on: bool,
        vector_on: bool,
        rerank_on: bool,
        recalled_count: int,
        reranked_count: int,
        selected_count: int,
        fallback_code: str | None,
        approved: dict[str, dict[str, Any]],
        total_ms: int,
    ) -> str | None:
        from app.models.context_engine import ContextRetrievalCandidate, ContextRetrievalRun
        from app.repositories.base import ensure_model_id

        if lexical_on and vector_on:
            retrieval_channel = "hybrid"
        elif lexical_on:
            retrieval_channel = "lexical"
        elif vector_on:
            retrieval_channel = "vector"
        else:
            retrieval_channel = "none"
        retrieval_policy_key = (
            "reranker"
            if rerank_on
            else "weighted_rrf" if lexical_on or vector_on else "none"
        )
        run = ContextRetrievalRun(
            public_id=_gen_public_id("crr_"),
            user_id=user_id,
            workspace_key=workspace_key,
            call_site="context.retrieval.knowledge",
            retrieval_channel=retrieval_channel,
            retrieval_policy_key=retrieval_policy_key,
            retrieval_policy_version="v1",
            query_hash=_query_hash(request.query_text),
            query_excerpt=request.query_text[:900],
            query_metadata_json={
                "source_family": request.source_family,
                "strategy": request.strategy.value if hasattr(request.strategy, "value") else str(request.strategy),
                "rerank_strategy": (
                    request.rerank_strategy.value
                    if hasattr(request.rerank_strategy, "value")
                    else str(request.rerank_strategy)
                ),
            },
            requested_candidate_limit=request.max_candidates or recalled_count,
            requested_final_limit=request.requested_final_limit or request.top_k,
            lexical_enabled=lexical_on,
            vector_enabled=vector_on,
            rerank_enabled=rerank_on,
            status="completed",
            fallback_code=fallback_code,
            recalled_count=recalled_count,
            reranked_count=reranked_count,
            selected_count=selected_count,
            total_latency_ms=total_ms,
            created_at=_utcnow(),
            completed_at=_utcnow(),
        )
        await ensure_model_id(self._session, ContextRetrievalRun, run)
        self._session.add(run)
        await self._session.flush()

        for m in approved.values():
            candidate = ContextRetrievalCandidate(
                retrieval_run_id=run.id,
                user_id=user_id,
                source_type="knowledge",
                source_public_id=m.get("source_public_id") or "",
                index_document_id=m.get("document_id"),
                index_chunk_id=m.get("chunk_id"),
                content_hash=m.get("content_hash") or "",
                content_excerpt=(m.get("content") or "")[:1000],
                estimated_tokens=m.get("estimated_tokens"),
                raw_rank=m.get("raw_rank"),
                raw_score=m.get("raw_score"),
                normalized_score=m.get("normalized_score"),
                rerank_score=m.get("rerank_score"),
                final_rank=m.get("final_rank"),
                selected=bool(m.get("selected")),
                drop_reason=m.get("drop_reason"),
                metadata_json={
                    key: m.get(key)
                    for key in ("channel", "channels", "channel_ranks", "channel_scores")
                    if m.get(key) is not None
                },
            )
            await ensure_model_id(self._session, ContextRetrievalCandidate, candidate)
            self._session.add(candidate)
        await self._session.flush()
        return run.public_id

    async def record_memory_run(
        self,
        *,
        user_id: int,
        workspace_key: str | None,
        scope_type: str,
        candidate_count: int,
        selected_count: int,
        latency_ms: int,
        drop_reason: str | None = None,
        fallback_code: str | None = None,
        requested_top_k: int = 5,
    ) -> str | None:
        """Memory 审计：只写统计字段，不写正文/Evidence。"""
        from app.models.context_engine import ContextRetrievalRun
        from app.repositories.base import ensure_model_id

        run = ContextRetrievalRun(
            public_id=_gen_public_id("crr_"),
            user_id=user_id,
            workspace_key=workspace_key,
            call_site="context.retrieval.memory",
            retrieval_channel="mysql_authoritative",
            retrieval_policy_key="memory_authoritative",
            retrieval_policy_version="v1",
            query_hash=_gen_public_id("")[:64],
            query_excerpt=None,
            query_metadata_json={
                "source_family": "memory",
                "scope_type": scope_type,
            },
            requested_candidate_limit=candidate_count,
            requested_final_limit=requested_top_k,
            lexical_enabled=False,
            vector_enabled=False,
            rerank_enabled=False,
            status="completed",
            fallback_code=fallback_code,
            recalled_count=candidate_count,
            reranked_count=candidate_count,
            selected_count=selected_count,
            total_latency_ms=latency_ms,
            created_at=_utcnow(),
            completed_at=_utcnow(),
        )
        await ensure_model_id(self._session, ContextRetrievalRun, run)
        self._session.add(run)
        await self._session.flush()
        return run.public_id


def _query_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
# auto-appended module-level note: 检索 audit: 记录每次 retrieve (query / hit_ids / latency / source)。
