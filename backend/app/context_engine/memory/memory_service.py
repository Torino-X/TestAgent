"""Memory 生命周期 Service：activate / reject / forget / delete / 提取。

CE-03 WP-6：
- activate：candidate → active（仅 API；auto-activate 仅 flag 门控）；
- reject：candidate → rejected；
- forget：status='forgotten' + deleted_at + archived_at；evidence_excerpt 清空；
  只保留无正文审计字段；缓存失效；
- delete：status='deleted'；正文/可恢复字段清空；context_memory_sources 物理
  删除；Payload 删除；保留最小 Tombstone 供历史 Ref 返回 deleted；
- 提取：memory.extract.* 经 ContextAwareLLMInvoker，产出 candidate；
  递归保护深度 >1 拒绝。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _gen_public_id(prefix: str) -> str:
    import uuid

    return prefix + uuid.uuid4().hex[:40]


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class MemoryServiceError(Exception):
    def __init__(self, detail: str, code: str = "context.memory.error") -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


class MemoryService:
    """Memory 生命周期操作（owner-scoped）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def activate(self, *, user_id: int, memory_public_id: str) -> dict[str, Any]:
        from app.repositories.context_engine_repositories import ContextMemoryRepository

        repo = ContextMemoryRepository(self._session)
        mem = await repo.get_by_public_id(memory_public_id, user_id)
        if mem is None:
            raise MemoryServiceError("记忆不存在或不属于当前用户", code="context.memory.not_found")
        if mem.status not in ("candidate", "active"):
            raise MemoryServiceError("当前状态不可激活", code="context.memory.invalid_transition")
        superseded_memory_id = None
        if mem.supersedes_memory_id is not None:
            previous = await repo.get_by_id(int(mem.supersedes_memory_id))
            if previous is not None and previous.user_id == user_id and previous.status in {"active", "candidate"}:
                # A replacement must never leave two contradictory versions of
                # the same durable preference eligible for retrieval.  Keep
                # the old row for audit/version history, but make it
                # ineligible for authoritative-memory selection.
                previous.status = "superseded"
                previous.archived_at = _utcnow()
                superseded_memory_id = previous.public_id
        mem.status = "active"
        mem.activation_source = "manual"
        await self._session.flush()
        return {
            "memory_public_id": mem.public_id,
            "status": mem.status,
            "superseded_memory_id": superseded_memory_id,
        }

    async def reject(self, *, user_id: int, memory_public_id: str) -> dict[str, Any]:
        from app.repositories.context_engine_repositories import ContextMemoryRepository

        repo = ContextMemoryRepository(self._session)
        mem = await repo.get_by_public_id(memory_public_id, user_id)
        if mem is None:
            raise MemoryServiceError("记忆不存在或不属于当前用户", code="context.memory.not_found")
        if mem.status != "candidate":
            raise MemoryServiceError("只有 candidate 可拒绝", code="context.memory.invalid_transition")
        mem.status = "rejected"
        await self._session.flush()
        return {"memory_public_id": mem.public_id, "status": mem.status}

    async def forget(self, *, user_id: int, memory_public_id: str) -> dict[str, Any]:
        """forget：status='forgotten' + deleted_at + archived_at；evidence 清空。"""
        from app.repositories.context_engine_repositories import (
            ContextMemoryRepository,
            ContextMemorySourceRepository,
        )

        repo = ContextMemoryRepository(self._session)
        mem = await repo.get_by_public_id(memory_public_id, user_id)
        if mem is None:
            raise MemoryServiceError("记忆不存在或不属于当前用户", code="context.memory.not_found")
        now = _utcnow()
        mem.status = "forgotten"
        mem.deleted_at = now
        mem.archived_at = now
        mem.content = ""
        mem.normalized_content = ""
        await self._session.flush()

        # evidence_excerpt 清空（只保留无正文审计字段）
        src_repo = ContextMemorySourceRepository(self._session)
        sources = await src_repo.list_for_memory(mem.id, user_id)
        for src in sources:
            src.evidence_excerpt = ""
        await self._session.flush()
        return {"memory_public_id": mem.public_id, "status": "forgotten"}

    async def delete(self, *, user_id: int, memory_public_id: str) -> dict[str, Any]:
        """delete：status='deleted'；正文清空；memory_sources 物理删除；
        Payload 删除；保留最小 Tombstone（供历史 Ref 返回 deleted）。"""
        from app.repositories.context_engine_repositories import (
            ContextMemoryRepository,
            ContextMemorySourceRepository,
        )

        repo = ContextMemoryRepository(self._session)
        mem = await repo.get_by_public_id(memory_public_id, user_id)
        if mem is None:
            raise MemoryServiceError("记忆不存在或不属于当前用户", code="context.memory.not_found")
        now = _utcnow()

        # memory_sources 物理删除（无软删列，唯一实现）
        src_repo = ContextMemorySourceRepository(self._session)
        sources = await src_repo.list_for_memory(mem.id, user_id)
        for src in sources:
            await self._session.delete(src)
        await self._session.flush()

        # 关联 Payload 删除（复用 ContextPayloadRepository 软删/物理删）
        await self._delete_related_payloads(mem, user_id)

        # Tombstone：status='deleted'，正文清空，保留审计字段
        mem.status = "deleted"
        mem.content = ""
        mem.normalized_content = ""
        mem.title = None
        mem.deleted_at = now
        mem.archived_at = now
        await self._session.flush()
        return {"memory_public_id": mem.public_id, "status": "deleted"}

    async def _delete_related_payloads(self, mem, user_id: int) -> None:
        from sqlalchemy import update
        from app.models.context_engine import ContextPayload

        # memory 相关 payload（source_type='memory'，source_public_id=mem.public_id）
        await self._session.execute(
            update(ContextPayload)
            .where(
                ContextPayload.user_id == user_id,
                ContextPayload.source_type == "memory",
                ContextPayload.source_public_id == mem.public_id,
                ContextPayload.deleted_at.is_(None),
            )
            .values(deleted_at=_utcnow())
        )

    # ── 提取（candidate，递归保护）────────────────────────────────

    async def create_candidate(
        self,
        *,
        user_id: int,
        scope_type: str,
        workspace_key: str | None,
        agent_type: str | None,
        memory_type: str,
        content: str,
        title: str | None = None,
        dedupe_key: str | None = None,
        evidence: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """创建 candidate 记忆（提取产物只写 candidate；深度 >1 拒绝由调用方 guard）。"""
        from app.repositories.context_engine_repositories import ContextMemoryRepository
        from app.models.context_engine import ContextMemorySource

        repo = ContextMemoryRepository(self._session)

        # dedup：content_hash / dedupe_key
        conflict = None
        if dedupe_key:
            conflict = await repo.find_conflict(
                user_id,
                scope_type,
                dedupe_key,
                workspace_key=workspace_key,
            )
        if conflict is None:
            all_memories = await repo.list_authoritative_active(
                user_id, scope_type=scope_type, workspace_key=workspace_key,
                agent_type=agent_type, now=_utcnow(), limit=50,
            )
            conflict = next((m for m in all_memories if m.content_hash == content_hash(content)), None)

        from app.models.context_engine import ContextMemory
        from app.repositories.base import ensure_model_id

        now = _utcnow()
        mem = ContextMemory(
            public_id=_gen_public_id("mem_"),
            user_id=user_id,
            scope_type=scope_type,
            workspace_key=workspace_key,
            agent_type=agent_type,
            memory_type=memory_type,
            title=title,
            content=content,
            normalized_content=content.strip().lower(),
            status="candidate",
            activation_source="auto",
            confidence=0.5,
            importance=3,
            content_hash=content_hash(content),
            dedupe_key=dedupe_key,
            idempotency_key=_gen_public_id("idem_"),
            version=(conflict.version + 1) if conflict else 1,
            supersedes_memory_id=conflict.id if conflict else None,
            created_by_type="system",
            created_by_user_id=user_id,
            # Older production schemas may not have the ORM-declared server
            # default.  Memory creation must therefore be self-contained.
            created_at=now,
            updated_at=now,
        )
        await ensure_model_id(self._session, ContextMemory, mem)
        self._session.add(mem)
        await self._session.flush()

        if evidence:
            for ev in evidence:
                src = ContextMemorySource(
                    memory_id=mem.id,
                    user_id=user_id,
                    source_type=ev.get("source_type") or "evidence",
                    source_public_id=ev.get("source_public_id"),
                    source_version=ev.get("source_version"),
                    relation_type=ev.get("relation_type") or "evidence",
                    source_digest=ev.get("source_digest"),
                    evidence_excerpt=(ev.get("evidence_excerpt") or "")[:1000],
                    created_at=_utcnow(),
                )
                await ensure_model_id(self._session, ContextMemorySource, src)
                self._session.add(src)
        await self._session.flush()
        return {
            "memory_public_id": mem.public_id,
            "status": mem.status,
            "supersedes_memory_id": mem.supersedes_memory_id,
        }
# auto-appended module-level note: memory service: 写入 + 读取 + scope 隔离。
