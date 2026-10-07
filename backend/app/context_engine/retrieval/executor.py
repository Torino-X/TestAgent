"""RetrievalExecutor：Knowledge 双通道召回 + 审计落库。

CE-03 WP-5：
- KnowledgeSourceAdapter 使用本 Executor；
- lexical（ES）+ dense（Qdrant）召回 → 一级外部 filter →
  MySQL 权威复核（二级 ACL）→ dedup → Weighted RRF / RerankService → top-k；
- 每 query 写 context_retrieval_runs + 每候选 context_retrieval_candidates；
- 无 Embedding Provider → dense disabled；无 Reranker → Weighted RRF fallback
  （不调用 Chat Model 冒充 Reranker）。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Protocol

from app.context_engine.indexing.stores_protocol import RetrievedChunk
from app.context_engine.models.retrieval import RetrievalRequest
from app.context_engine.retrieval.retrieval import (
    FusionResult,
    mysql_authoritative_recheck,
    query_hash,
    weighted_rrf,
)

logger = logging.getLogger(__name__)


class EmbeddingProviderLike(Protocol):
    async def embed(self, request: Any) -> Any: ...


class RerankServiceLike(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def rerank(self, request: Any) -> Any: ...


class RetrievalExecutor:
    """双通道检索执行器。

    构造注入（DI）：
    - vector_store / lexical_store：VectorStoreProtocol / LexicalStoreProtocol；
    - embedding_provider：EmbeddingProviderProtocol | None（None → dense disabled）；
    - embedding_dimension：int | None；
    - rerank_service：RerankServiceProtocol | None（None → Weighted RRF）。
    """

    def __init__(
        self,
        *,
        vector_store=None,
        lexical_store=None,
        embedding_provider: EmbeddingProviderLike | None = None,
        embedding_dimension: int | None = None,
        embedding_model: str | None = None,
        embedding_namespace: str | None = None,
        lexical_namespace: str | None = None,
        rerank_service: RerankServiceLike | None = None,
    ) -> None:
        self._vector_store = vector_store
        self._lexical_store = lexical_store
        self._embedding_provider = embedding_provider
        self._embedding_dimension = embedding_dimension
        self._embedding_model = embedding_model
        self._embedding_namespace = embedding_namespace
        self._lexical_namespace = lexical_namespace
        self._rerank_service = rerank_service

    @property
    def dense_enabled(self) -> bool:
        return bool(
            self._vector_store is not None
            and getattr(self._vector_store, "enabled", False)
            and self._embedding_provider is not None
            and self._embedding_dimension
            and self._embedding_namespace
        )

    @property
    def lexical_enabled(self) -> bool:
        return bool(
            self._lexical_store is not None
            and getattr(self._lexical_store, "enabled", False)
            and self._lexical_namespace
        )

    async def execute(
        self,
        *,
        request: RetrievalRequest,
        session,
        user_internal_id: int,
        workspace_key: str | None,
        agent_type: str | None = None,
        lexical_allowed: bool = True,
        vector_allowed: bool = True,
        hybrid_allowed: bool = True,
        rerank_allowed: bool = True,
    ) -> tuple[list[FusionResult], str | None]:
        """执行一次检索。

        返回 (approved fused results, run_public_id)。
        全部通道不可用或无命中也会写审计 run，并返回其 public_id。
        """
        started = time.monotonic()
        recall_chunks: list[RetrievedChunk] = []
        fallback_code: str | None = None
        candidate_limit = request.max_candidates or request.top_k
        lexical_on = bool(lexical_allowed and self.lexical_enabled)
        vector_on = bool(vector_allowed and self.dense_enabled)
        if lexical_on and vector_on and not hybrid_allowed:
            # 单通道模式优先 lexical；dense 在 lexical 被禁用/不可用时仍可独立工作。
            vector_on = False

        if not lexical_on and not vector_on:
            fallback_code = (
                "context.retrieval.no_channel_enabled"
                if not lexical_allowed and not vector_allowed
                else "context.retrieval.channel_unavailable"
            )
            run_id = await self._audit_empty(
                session=session,
                request=request,
                user_internal_id=user_internal_id,
                workspace_key=workspace_key,
                lexical_on=False,
                vector_on=False,
                fallback_code=fallback_code,
                started=started,
            )
            return [], run_id

        logger.info(
            "DIAG | RetrievalExecutor.execute 开始 | "
            "lexical_on=%s (store=%s, namespace=%s) | "
            "vector_on=%s (store=%s, namespace=%s, dim=%s) | "
            "query_text=%r",
            lexical_on,
            self._lexical_store is not None,
            self._lexical_namespace,
            vector_on,
            self._vector_store is not None,
            self._embedding_namespace,
            self._embedding_dimension,
            (request.query_text or "")[:200],
        )

        import asyncio

        async def _lexical_recall() -> list[RetrievedChunk]:
            return await self._lexical_store.search(
                namespace=self._lexical_namespace,
                query_text=request.query_text,
                user_id=user_internal_id,
                workspace_key=workspace_key,
                top_k=candidate_limit,
            )

        async def _dense_recall() -> tuple[list[RetrievedChunk], str | None] | None:
            vector = await self._embed_query(request.query_text)
            if vector is None:
                return None
            return await self._vector_store.search(
                namespace=self._embedding_namespace,
                vector=vector,
                user_id=user_internal_id,
                workspace_key=workspace_key,
                top_k=candidate_limit,
            ), None

        tasks: list[asyncio.Task] = []
        task_kind: list[str] = []  # 与 tasks 对齐：'lexical' | 'dense'
        if lexical_on:
            tasks.append(asyncio.create_task(_lexical_recall()))
            task_kind.append("lexical")
        if vector_on:
            tasks.append(asyncio.create_task(_dense_recall()))
            task_kind.append("dense")

        results: list[Any] = [None] * len(tasks)
        for i, t in enumerate(tasks):
            try:
                results[i] = await t
            except Exception as exc:  # noqa: BLE001
                if task_kind[i] == "lexical":
                    lexical_on = False
                    fallback_code = "context.retrieval.lexical_unavailable"
                else:
                    vector_on = False
                    fallback_code = "context.retrieval.vector_unavailable"
                logger.warning("recall failed: %s", type(exc).__name__)

        for i, kind in enumerate(task_kind):
            res = results[i]
            if res is None:
                if kind == "dense":
                    # A failed vector-store task is also represented by None,
                    # but its exception was already classified above as
                    # ``vector_unavailable``.  Only classify this as an
                    # embedding problem when the dense channel was still on;
                    # otherwise the post-processing loop would overwrite the
                    # original, more precise fault code.
                    if vector_on:
                        vector_on = False
                        fallback_code = "context.retrieval.embedding_unavailable"
                continue
            if kind == "lexical":
                recall_chunks.extend(res)
            else:
                # dense：res 为 (hits, None) 或 None
                if res is None or res[0] is None:
                    vector_on = False
                    fallback_code = "context.retrieval.embedding_unavailable"
                else:
                    recall_chunks.extend(res[0])

        # ── 两路召回结果统计（DIAG）──
        logger.info(
            "DIAG | RetrievalExecutor 召回统计 | "
            "recall_chunks_total=%d | lexical_on=%s | vector_on=%s | "
            "fallback_code=%s | results_count=%d",
            len(recall_chunks), lexical_on, vector_on, fallback_code, len(results),
        )

        if not recall_chunks:
            run_id = await self._audit_empty(
                session=session,
                request=request,
                user_internal_id=user_internal_id,
                workspace_key=workspace_key,
                lexical_on=lexical_on,
                vector_on=vector_on,
                fallback_code=fallback_code or "context.retrieval.no_hits",
                started=started,
            )
            return [], run_id

        # 第二级 MySQL 权威复核
        channels_by_chunk: dict[str, set[str]] = {}
        for c in recall_chunks:
            channels_by_chunk.setdefault(c.chunk_public_id, set()).add(c.channel)
        recheck = await mysql_authoritative_recheck(
            session=session,
            chunk_public_ids=[c.chunk_public_id for c in recall_chunks],
            user_id=user_internal_id,
            workspace_key=workspace_key,
            lexical_channel=lexical_on,
            vector_channel=vector_on,
            channels_by_chunk=channels_by_chunk,
            allowed_source_types=request.source_types,
            allowed_source_public_ids=request.source_public_ids,
            allowed_document_public_ids=request.document_public_ids,
            allowed_source_versions=request.source_versions,
        )
        approved_map = {a["chunk_public_id"]: a for a in recheck.approved}
        audit_candidates = _audit_candidates_from_recall(recall_chunks)
        for chunk_public_id, observed in recheck.observed_by_chunk.items():
            audit_candidates.setdefault(chunk_public_id, {}).update(observed)
        for chunk_public_id, approved in approved_map.items():
            audit_candidates.setdefault(chunk_public_id, {}).update(approved)
        for chunk_public_id, reason in recheck.rejected_by_chunk.items():
            audit_candidates.setdefault(chunk_public_id, {"chunk_public_id": chunk_public_id})[
                "drop_reason"
            ] = reason

        # dedup：chunk_public_id
        seen: set[str] = set()
        deduped: list[RetrievedChunk] = []
        for c in recall_chunks:
            if c.chunk_public_id in seen or c.chunk_public_id not in approved_map:
                continue
            seen.add(c.chunk_public_id)
            deduped.append(c)

        if not deduped:
            run_id = await self._audit_empty(
                session=session,
                request=request,
                user_internal_id=user_internal_id,
                workspace_key=workspace_key,
                lexical_on=lexical_on,
                vector_on=vector_on,
                fallback_code=fallback_code or "context.retrieval.acl_filtered",
                started=started,
                recalled_count=len(recall_chunks),
                approved=audit_candidates,
            )
            return [], run_id

        # 通道分组 → 融合
        lexical_list = [c for c in deduped if c.channel == "lexical"]
        vector_list = [c for c in deduped if c.channel == "vector"]
        lists = []
        if lexical_list:
            lists.append([_to_fusion(c, approved_map) for c in lexical_list])
        if vector_list:
            lists.append([_to_fusion(c, approved_map) for c in vector_list])

        if not lists:
            run_id = await self._audit_empty(
                session=session,
                request=request,
                user_internal_id=user_internal_id,
                workspace_key=workspace_key,
                lexical_on=lexical_on,
                vector_on=vector_on,
                fallback_code=fallback_code or "context.retrieval.no_approved_channel",
                started=started,
                recalled_count=len(recall_chunks),
                approved=audit_candidates,
            )
            return [], run_id

        # Fetch enough candidates from each channel for reranking, then bound the
        # total pool before the provider call.  ``top_k`` remains the final
        # context limit; ``max_candidates`` is the pre-rerank candidate budget.
        fused_all = weighted_rrf(lists)
        for item in fused_all:
            entry = audit_candidates.setdefault(item.chunk_public_id, {})
            entry["normalized_score"] = item.normalized_score
        fused = fused_all[:candidate_limit]
        for item in fused_all[candidate_limit:]:
            audit_candidates.setdefault(item.chunk_public_id, {})["drop_reason"] = "candidate_limit"
        substantive = [item for item in fused if not _is_non_evidentiary_candidate(item.content_excerpt)]
        for item in fused:
            if item not in substantive:
                audit_candidates.setdefault(item.chunk_public_id, {})["drop_reason"] = "non_evidentiary"
        if substantive and len(substantive) != len(fused):
            logger.info(
                "Filtered %d non-evidentiary retrieval candidates before rerank",
                len(fused) - len(substantive),
            )
            fused = substantive

        # Reranker（无 reranker → Weighted RRF fallback，不冒充）
        rerank_enabled = bool(
            rerank_allowed
            and self._rerank_service is not None
            and getattr(self._rerank_service, "enabled", False)
        )
        rerank_executed = False
        reranked = fused
        rerank_requested = request.rerank_strategy.value == "reranker"
        if rerank_enabled and rerank_requested:
            try:
                reranked = await self._apply_rerank(request, fused)
                rerank_executed = True
            except Exception as exc:  # noqa: BLE001
                fallback_code = "context.retrieval.reranker_unavailable"
                logger.warning("rerank failed; using weighted RRF: %s", type(exc).__name__)
        elif rerank_requested:
            fallback_code = fallback_code or "context.retrieval.reranker_unavailable"

        top = reranked[: request.requested_final_limit or request.top_k]

        # 审计只记录既有决策，不干预候选顺序或选择结果。
        for m in reranked:
            entry = audit_candidates.setdefault(m.chunk_public_id, {})
            entry["final_rank"] = m.final_rank
            entry["normalized_score"] = m.normalized_score
            entry["rerank_score"] = m.rerank_score
            if m not in top:
                entry["drop_reason"] = "rerank_cutoff" if rerank_executed else "top_k"
        for m in top:
            entry = audit_candidates.setdefault(m.chunk_public_id, {})
            entry["final_rank"] = m.final_rank
            entry["selected"] = True
            entry["drop_reason"] = None
            entry["normalized_score"] = m.normalized_score
            entry["rerank_score"] = m.rerank_score

        run_id = await self._audit(
            session=session,
            request=request,
            user_internal_id=user_internal_id,
            workspace_key=workspace_key,
            lexical_on=lexical_on,
            vector_on=vector_on,
            rerank_on=rerank_executed,
            recalled_count=len(deduped),
            reranked_count=len(reranked),
            selected_count=len(top),
            fallback_code=fallback_code,
            approved=audit_candidates,
            total_ms=int((time.monotonic() - started) * 1000),
        )
        return top, run_id

    async def _audit_empty(
        self,
        *,
        session,
        request: RetrievalRequest,
        user_internal_id: int,
        workspace_key: str | None,
        lexical_on: bool,
        vector_on: bool,
        fallback_code: str,
        started: float,
        recalled_count: int = 0,
        approved: dict[str, dict[str, Any]] | None = None,
    ) -> str | None:
        return await self._audit(
            session=session,
            request=request,
            user_internal_id=user_internal_id,
            workspace_key=workspace_key,
            lexical_on=lexical_on,
            vector_on=vector_on,
            rerank_on=False,
            recalled_count=recalled_count,
            reranked_count=0,
            selected_count=0,
            fallback_code=fallback_code,
            approved=approved or {},
            total_ms=int((time.monotonic() - started) * 1000),
        )

    async def _embed_query(self, query_text: str) -> list[float] | None:
        from app.context_engine.providers.protocols import EmbeddingRequest, EmbeddingText

        if not self._embedding_provider or not self._embedding_dimension:
            return None
        result = await self._embedding_provider.embed(
            EmbeddingRequest(
                request_id=_gen_rid(),
                model=self._embedding_model or "",
                input_type="query",
                texts=(EmbeddingText(text_id="q", text=query_text),),
                dimension=self._embedding_dimension,
                normalize=True,
            )
        )
        if not result.vectors:
            return None
        vec = result.vectors[0]
        # dimension mismatch 验证：不符 → None（不补零/截断）
        if len(vec.values) != self._embedding_dimension:
            logger.warning("embedding dimension mismatch: got %d expect %d", len(vec.values), self._embedding_dimension)
            return None
        return vec.values

    async def _apply_rerank(self, request: RetrievalRequest, fused: list[FusionResult]) -> list[FusionResult]:
        from app.context_engine.providers.protocols import RerankItem, RerankRequest

        items = [
            RerankItem(doc_id=m.chunk_public_id, text=m.content_excerpt or m.chunk_public_id)
            for m in fused
        ]
        result = await self._rerank_service.rerank(
            RerankRequest(
                request_id=_gen_rid(),
                model="",
                query=request.query_text,
                items=tuple(items),
            )
        )
        score_map = {s.doc_id: s.score for s in result.scores}
        scored = []
        for m in fused:
            scored.append(FusionResult(
                chunk_public_id=m.chunk_public_id,
                document_public_id=m.document_public_id,
                user_id=m.user_id,
                workspace_key=m.workspace_key,
                final_rank=m.final_rank,
                raw_score=m.raw_score,
                normalized_score=m.normalized_score,
                rerank_score=score_map.get(m.chunk_public_id, 0.0),
                channel=m.channel,
                content_excerpt=m.content_excerpt,
                source_public_id=m.source_public_id,
                source_version=m.source_version,
            ))
        reranked = sorted(scored, key=lambda m: m.rerank_score or 0.0, reverse=True)
        for i, m in enumerate(reranked, start=1):
            reranked[i - 1] = FusionResult(
                chunk_public_id=m.chunk_public_id,
                document_public_id=m.document_public_id,
                user_id=m.user_id,
                workspace_key=m.workspace_key,
                final_rank=i,
                raw_score=m.raw_score,
                normalized_score=m.normalized_score,
                rerank_score=m.rerank_score,
                channel=m.channel,
                content_excerpt=m.content_excerpt,
                source_public_id=m.source_public_id,
                source_version=m.source_version,
            )
        return reranked

    async def _audit(
        self,
        *,
        session,
        request: RetrievalRequest,
        user_internal_id: int,
        workspace_key: str | None,
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
        from app.context_engine.retrieval.audit import RetrievalAuditService

        service = RetrievalAuditService(session)
        return await service.record_knowledge_run(
            user_id=user_internal_id,
            workspace_key=workspace_key,
            request=request,
            lexical_on=lexical_on,
            vector_on=vector_on,
            rerank_on=rerank_on,
            recalled_count=recalled_count,
            reranked_count=reranked_count,
            selected_count=selected_count,
            fallback_code=fallback_code,
            approved=approved,
            total_ms=total_ms,
        )


def _to_fusion(c: RetrievedChunk, approved: dict[str, dict[str, Any]]) -> FusionResult:
    a = approved.get(c.chunk_public_id, {})
    return FusionResult(
        chunk_public_id=c.chunk_public_id,
        document_public_id=c.document_public_id or a.get("document_public_id"),
        user_id=c.user_id,
        workspace_key=c.workspace_key or a.get("workspace_key"),
        final_rank=c.rank,
        raw_score=c.score,
        normalized_score=c.score,
        rerank_score=None,
        channel=c.channel,
        content_excerpt=a.get("content"),
        source_public_id=a.get("source_public_id"),
        source_version=a.get("source_version"),
    )


def _audit_candidates_from_recall(recall_chunks: list[RetrievedChunk]) -> dict[str, dict[str, Any]]:
    """Capture per-channel recall evidence without affecting the pipeline."""
    candidates: dict[str, dict[str, Any]] = {}
    for chunk in recall_chunks:
        entry = candidates.setdefault(
            chunk.chunk_public_id,
            {
                "chunk_public_id": chunk.chunk_public_id,
                "source_public_id": chunk.source_public_id,
                "document_public_id": chunk.document_public_id,
                "raw_rank": chunk.rank,
                "raw_score": chunk.score,
                "channel": chunk.channel,
                "channels": [],
                "channel_ranks": {},
                "channel_scores": {},
            },
        )
        entry["raw_rank"] = min(entry.get("raw_rank", chunk.rank), chunk.rank)
        entry["raw_score"] = max(entry.get("raw_score", chunk.score), chunk.score)
        entry["channels"] = sorted(set(entry.get("channels", [])) | {chunk.channel})
        entry["channel_ranks"][chunk.channel] = chunk.rank
        entry["channel_scores"][chunk.channel] = chunk.score
        if len(entry["channels"]) > 1:
            entry["channel"] = "hybrid"
    return candidates


def _is_non_evidentiary_candidate(content: str | None) -> bool:
    """Exclude structural text and text that explicitly disqualifies itself."""
    if not content:
        return False
    body = re.sub(r"^\s*<section:[^>]+>", "", content).strip()
    if not body:
        return True
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if lines and all(line.startswith("#") for line in lines):
        return True
    if len(lines) == 1 and bool(re.fullmatch(r"[^。！？.!?\n]{1,80}[:：]", body)):
        return True

    normalized = re.sub(r"\s+", "", body)
    return (
        ("不应被选中" in normalized or "不需要被选中" in normalized)
        and ("无关" in normalized or "不相关" in normalized)
    )


def _gen_rid() -> str:
    import uuid

    return uuid.uuid4().hex
# auto-appended module-level note: 检索执行器: 串行/并行多源检索 + rerank。
