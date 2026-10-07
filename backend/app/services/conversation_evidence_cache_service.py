"""Bounded, version-aware reusable evidence for one conversation.

The cache is a candidate recall layer, not a replacement for RAG.  Every
materialization receives fresh retrieval candidates first, persists the ones
actually eligible for reuse, then merges valid cached candidates with them.
That prevents the common but unsafe "cache hit means do not retrieve" design.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.context_engine import (
    ContextIndexChunk,
    ContextIndexDocument,
    ConversationEvidenceAudit,
    ConversationEvidenceCache,
)
from app.repositories.base import ensure_model_id
from app.utils.ids import generate_public_id


MAX_CACHE_ITEMS = 12
MAX_CACHE_TOKENS = 8_000
MAX_PROJECT_CONTEXT_ITEMS = 8
MAX_PROJECT_CONTEXT_TOKENS = 6_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ConversationEvidenceCacheService:
    """Materialize and govern a conversation's project-evidence working set."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def merge_with_retrieval(
        self,
        *,
        user_id: int,
        conversation_id: int,
        workspace_key: str,
        retrieval_hits: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Persist fresh hits, invalidate changed sources, then return a budgeted merge."""
        now = _utcnow()
        fresh_chunk_ids: set[str] = set()
        for hit in retrieval_hits:
            row = await self._resolve_hit(user_id=user_id, workspace_key=workspace_key, hit=hit)
            if row is None:
                continue
            chunk, document = row
            fresh_chunk_ids.add(chunk.public_id)
            await self._upsert_candidate(
                user_id=user_id,
                conversation_id=conversation_id,
                chunk=chunk,
                document=document,
                relevance_score=float(hit.get("score") or 0),
                now=now,
            )

        active = await self._valid_active_rows(
            user_id=user_id,
            conversation_id=conversation_id,
            workspace_key=workspace_key,
            now=now,
        )
        await self._enforce_limits(active, user_id=user_id, conversation_id=conversation_id, now=now)
        active = [row for row in active if row.status == "active"]
        selected = self._select(active, fresh_chunk_ids)
        for row in selected:
            row.selected_count = int(row.selected_count or 0) + 1
            row.last_used_at = now
            await self._audit(
                user_id=user_id,
                conversation_id=conversation_id,
                cache=row,
                action="cache_hit",
                reason=(
                    "fresh_retrieval"
                    if str((row.locator_json or {}).get("chunk_id") or "") in fresh_chunk_ids
                    else "reusable_evidence"
                ),
            )
        await self._session.flush()
        return [self._as_source_hit(row) for row in selected]

    async def set_pinned(
        self,
        *,
        user_id: int,
        conversation_id: int,
        evidence_public_id: str,
        pinned: bool,
    ) -> ConversationEvidenceCache | None:
        row = (await self._session.execute(
            select(ConversationEvidenceCache).where(
                ConversationEvidenceCache.public_id == evidence_public_id,
                ConversationEvidenceCache.user_id == user_id,
                ConversationEvidenceCache.conversation_id == conversation_id,
                ConversationEvidenceCache.status == "active",
            )
        )).scalar_one_or_none()
        if row is None:
            return None
        row.pinned = pinned
        row.last_used_at = _utcnow()
        await self._audit(
            user_id=user_id,
            conversation_id=conversation_id,
            cache=row,
            action="pinned" if pinned else "unpinned",
            reason="user_action",
        )
        await self._session.flush()
        return row

    async def _resolve_hit(self, *, user_id: int, workspace_key: str, hit: dict[str, Any]):
        chunk_id = str(hit.get("chunk_id") or "")
        if not chunk_id:
            return None
        result = await self._session.execute(
            select(ContextIndexChunk, ContextIndexDocument)
            .join(ContextIndexDocument, ContextIndexDocument.id == ContextIndexChunk.document_id)
            .where(
                ContextIndexChunk.public_id == chunk_id,
                ContextIndexChunk.user_id == user_id,
                ContextIndexChunk.status == "active",
                ContextIndexChunk.deleted_at.is_(None),
                ContextIndexDocument.user_id == user_id,
                ContextIndexDocument.workspace_key == workspace_key,
                ContextIndexDocument.status == "indexed",
                ContextIndexDocument.deleted_at.is_(None),
            )
        )
        return result.first()

    async def _upsert_candidate(
        self,
        *,
        user_id: int,
        conversation_id: int,
        chunk: ContextIndexChunk,
        document: ContextIndexDocument,
        relevance_score: float,
        now: datetime,
    ) -> ConversationEvidenceCache:
        row = (await self._session.execute(
            select(ConversationEvidenceCache).where(
                ConversationEvidenceCache.conversation_id == conversation_id,
                ConversationEvidenceCache.index_chunk_id == chunk.id,
            )
        )).scalar_one_or_none()
        if row is None:
            row = ConversationEvidenceCache(
                public_id=generate_public_id("ctxev"),
                user_id=user_id,
                conversation_id=conversation_id,
                index_document_id=document.id,
                index_chunk_id=chunk.id,
                source_type=document.source_type,
                source_public_id=document.source_public_id,
                source_version=document.source_version,
                source_digest=document.source_digest,
                content_hash=chunk.content_hash,
                title=document.title,
                section_path=chunk.section_path,
                content_excerpt=str(chunk.content or "")[:1600],
                estimated_tokens=int(chunk.estimated_tokens or max(1, len(chunk.content or "") // 3)),
                relevance_score=relevance_score,
                status="active",
                selected_count=0,
                last_used_at=now,
                locator_json={"chunk_id": chunk.public_id, "document_id": document.public_id, "section": chunk.section_path},
            )
            await ensure_model_id(self._session, ConversationEvidenceCache, row)
            self._session.add(row)
            await self._session.flush()
            await self._audit(user_id=user_id, conversation_id=conversation_id, cache=row, action="cached", reason="fresh_retrieval")
            return row
        row.source_version = document.source_version
        row.source_digest = document.source_digest
        row.content_hash = chunk.content_hash
        row.title = document.title
        row.section_path = chunk.section_path
        row.content_excerpt = str(chunk.content or "")[:1600]
        row.estimated_tokens = int(chunk.estimated_tokens or max(1, len(chunk.content or "") // 3))
        row.relevance_score = relevance_score
        row.status = "active"
        row.evicted_at = None
        row.last_used_at = now
        return row

    async def _valid_active_rows(self, *, user_id: int, conversation_id: int, workspace_key: str, now: datetime) -> list[ConversationEvidenceCache]:
        rows = list((await self._session.execute(
            select(ConversationEvidenceCache, ContextIndexChunk, ContextIndexDocument)
            .join(ContextIndexChunk, ContextIndexChunk.id == ConversationEvidenceCache.index_chunk_id)
            .join(ContextIndexDocument, ContextIndexDocument.id == ConversationEvidenceCache.index_document_id)
            .where(
                ConversationEvidenceCache.user_id == user_id,
                ConversationEvidenceCache.conversation_id == conversation_id,
                ConversationEvidenceCache.status == "active",
            )
        )).all())
        valid: list[ConversationEvidenceCache] = []
        for cache, chunk, document in rows:
            unchanged = (
                chunk.status == "active"
                and chunk.deleted_at is None
                and document.status == "indexed"
                and document.deleted_at is None
                and document.workspace_key == workspace_key
                and cache.source_version == document.source_version
                and cache.source_digest == document.source_digest
                and cache.content_hash == chunk.content_hash
            )
            if unchanged:
                valid.append(cache)
                continue
            cache.status = "stale"
            cache.evicted_at = now
            await self._audit(user_id=user_id, conversation_id=conversation_id, cache=cache, action="stale", reason="source_changed")
        await self._session.flush()
        return valid

    async def _enforce_limits(self, rows: list[ConversationEvidenceCache], *, user_id: int, conversation_id: int, now: datetime) -> None:
        active = list(rows)
        def over() -> bool:
            return len(active) > MAX_CACHE_ITEMS or sum(int(row.estimated_tokens or 0) for row in active) > MAX_CACHE_TOKENS
        while over():
            evictable = [row for row in active if not row.pinned]
            if not evictable:
                return
            victim = min(
                evictable,
                key=lambda row: (row.last_used_at or row.created_at, float(row.relevance_score or 0), row.id),
            )
            victim.status = "evicted"
            victim.evicted_at = now
            active.remove(victim)
            await self._audit(user_id=user_id, conversation_id=conversation_id, cache=victim, action="evicted", reason="capacity_limit")
        await self._session.flush()

    @staticmethod
    def _select(rows: list[ConversationEvidenceCache], fresh_chunk_ids: set[str]) -> list[ConversationEvidenceCache]:
        ordered = sorted(
            rows,
            key=lambda row: (
                0 if row.pinned else 1,
                0 if str((row.locator_json or {}).get("chunk_id") or "") in fresh_chunk_ids else 1,
                -float(row.relevance_score or 0),
                -(row.selected_count or 0),
                row.id,
            ),
        )
        selected: list[ConversationEvidenceCache] = []
        tokens = 0
        for row in ordered:
            estimate = int(row.estimated_tokens or 0)
            if selected and (len(selected) >= MAX_PROJECT_CONTEXT_ITEMS or tokens + estimate > MAX_PROJECT_CONTEXT_TOKENS):
                continue
            selected.append(row)
            tokens += estimate
        return selected

    @staticmethod
    def _as_source_hit(row: ConversationEvidenceCache) -> dict[str, Any]:
        locator = dict(row.locator_json or {})
        return {
            "chunk_id": locator.get("chunk_id"),
            "document_id": locator.get("document_id"),
            "source_id": row.source_public_id,
            "title": row.title,
            "section": row.section_path,
            "content": row.content_excerpt,
            "score": float(row.relevance_score or 0),
            "source_role": "generated_artifact" if row.source_type == "artifact" else "project_source",
            "is_current": True,
            "authority": 30 if row.source_type == "artifact" else 100,
            "evidence_cache_id": row.public_id,
            "pinned": bool(row.pinned),
        }

    async def _audit(self, *, user_id: int, conversation_id: int, cache: ConversationEvidenceCache, action: str, reason: str | None) -> None:
        row = ConversationEvidenceAudit(
            public_id=generate_public_id("ctxeva"),
            user_id=user_id,
            conversation_id=conversation_id,
            evidence_cache_id=cache.id,
            action=action,
            reason=reason,
            details_json={
                "evidence_id": cache.public_id,
                "document_id": (cache.locator_json or {}).get("document_id"),
                "chunk_id": (cache.locator_json or {}).get("chunk_id"),
                "source_version": cache.source_version,
            },
        )
        await ensure_model_id(self._session, ConversationEvidenceAudit, row)
        self._session.add(row)


__all__ = [
    "ConversationEvidenceCacheService",
    "MAX_CACHE_ITEMS",
    "MAX_CACHE_TOKENS",
]
