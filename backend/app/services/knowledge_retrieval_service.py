"""Read-only Company RAG retrieval and response normalisation."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoError, decrypt_api_key
from app.integrations.knowledge_base_client import build_company_rag_client
from app.repositories.knowledge_config_repository import KnowledgeConfigRepository


@dataclass(slots=True)
class KnowledgeHit:
    knowledge_id: str | None = None
    doc_id: str | None = None
    doc_name: str | None = None
    chunk_id: str | None = None
    chunk_title: str | None = None
    content: str = ""
    score: float | None = None
    labels: list[dict[str, Any]] = field(default_factory=list)
    source_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class KnowledgeRetrieveResult:
    success: bool
    hits: list[KnowledgeHit] = field(default_factory=list)
    elapsed_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None


class KnowledgeRetrievalService:
    """Compatibility name for the generic Company RAG search service."""

    def __init__(self, session: AsyncSession) -> None:
        self._repo = KnowledgeConfigRepository(session)

    async def retrieve(
        self,
        *,
        user_internal_id: int,
        query: str,
        top_k: int | None = None,
        similarity_threshold: float | None = None,
        retrieve_strategy: int | None = None,
        **_ignored: Any,
    ) -> KnowledgeRetrieveResult:
        query = (query or "").strip()
        if not query:
            return KnowledgeRetrieveResult(False, error_code="COMPANY_RAG_QUERY_EMPTY", error_message="Query cannot be empty.")
        row = await self._repo.get_for_user(user_internal_id)
        if row is None or not row.test_plan_generation_enabled:
            return KnowledgeRetrieveResult(False, error_code="COMPANY_RAG_NOT_CONFIGURED", error_message="Company RAG is not enabled.")
        try:
            api_key = decrypt_api_key(row.api_key_encrypted or "")
        except CryptoError:
            return KnowledgeRetrieveResult(False, error_code="COMPANY_RAG_API_KEY_INVALID", error_message="Stored API key cannot be decrypted.")
        client = build_company_rag_client(api_base_url=row.api_base_url, api_key=api_key, timeout_seconds=int(row.timeout_seconds or 30))
        started = time.monotonic()
        try:
            upstream = await client.retrieve_chunks({
                "query": query,
                "topK": self._bounded_int(top_k, int(row.top_k or 5), 1, 20),
                "similarityThreshold": self._bounded_float(similarity_threshold, float(row.similarity_threshold or 0.35), 0.0, 1.0),
                "retrieveStrategy": self._bounded_int(retrieve_strategy, int(row.retrieve_strategy or 3), 1, 3),
            })
        finally:
            await client.aclose()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if not upstream.success:
            return KnowledgeRetrieveResult(False, elapsed_ms=elapsed_ms, error_code=upstream.error_code, error_message=upstream.error_message)
        return KnowledgeRetrieveResult(True, hits=self._normalise_hits(upstream.data), elapsed_ms=elapsed_ms)

    @staticmethod
    def _normalise_hits(data: Any) -> list[KnowledgeHit]:
        if isinstance(data, dict):
            candidates = data.get("hits") or data.get("results") or data.get("list") or data.get("items") or []
        else:
            candidates = data
        if not isinstance(candidates, list):
            return []
        hits: list[KnowledgeHit] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or item.get("text") or item.get("chunkContent") or "").strip()
            if not content:
                continue
            score = item.get("score") or item.get("similarity")
            try:
                score = float(score) if score is not None else None
            except (TypeError, ValueError):
                score = None
            hits.append(KnowledgeHit(
                knowledge_id=str(item.get("knowledgeId") or item.get("knowledge_id") or "") or None,
                doc_id=str(item.get("docId") or item.get("doc_id") or "") or None,
                doc_name=str(item.get("docName") or item.get("doc_name") or item.get("title") or "") or None,
                chunk_id=str(item.get("chunkId") or item.get("chunk_id") or item.get("id") or "") or None,
                chunk_title=str(item.get("chunkTitle") or item.get("chunk_title") or item.get("title") or "") or None,
                content=content,
                score=score,
                source_type="company_rag",
                metadata={k: v for k, v in item.items() if k not in {"content", "text", "chunkContent"}},
            ))
        return hits

    @staticmethod
    def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(value if value is not None else default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _bounded_float(value: Any, default: float, low: float, high: float) -> float:
        try:
            return max(low, min(high, float(value if value is not None else default)))
        except (TypeError, ValueError):
            return default
