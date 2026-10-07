"""CE-03 整改2：Anti-Rehydration 5 项（Leakage=0）。

逐项验证（不只验证 MemorySourceAdapter 查询为空）：
1. Forgotten Retrieval = 0           —— forgotten 记忆不再被权威检索返回
2. Deleted Retrieval = 0             —— deleted 记忆不再被权威检索返回
3. Snapshot Ref Rehydrate = 0        —— 已删记忆的 Ref 经 Ref Resolver 不能恢复正文
4. Payload Rehydrate = 0             —— 关联 Payload 删除后读取/列表不可达
5. Source Evidence Rehydrate = 0     —— evidence_excerpt 清空后不可读回

真实数据库结果（SQLite 内存库，与 MySQL 同 ORM 语义）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.context_engine.memory import MemoryService


async def _seed_memory(sf, *, public_id, user_id=1, scope_type="user",
                       workspace_key=None, agent_type=None, status="active",
                       content="secret evidence", evidence_excerpt="evidence secret",
                       deleted_at=None, archived_at=None):
    from app.models.context_engine import ContextMemory, ContextMemorySource
    from app.repositories.base import ensure_model_id

    async with sf() as session:
        mem = ContextMemory(
            public_id=public_id, user_id=user_id, scope_type=scope_type,
            workspace_key=workspace_key, agent_type=agent_type,
            memory_type="fact", content=content, status=status,
            activation_source="manual", confidence=0.5, importance=3,
            content_hash="h_" + public_id, idempotency_key="i_" + public_id,
            version=1, created_by_user_id=user_id,
            valid_from=datetime.now() - timedelta(days=1),
            expires_at=datetime.now() + timedelta(days=1),
            deleted_at=deleted_at, archived_at=archived_at,
        )
        await ensure_model_id(session, ContextMemory, mem)
        session.add(mem)
        await session.flush()
        src = ContextMemorySource(
            memory_id=mem.id, user_id=user_id, source_type="evidence",
            source_public_id="src_" + public_id, relation_type="evidence",
            evidence_excerpt=evidence_excerpt,
        )
        await ensure_model_id(session, ContextMemorySource, src)
        session.add(src)
        await session.commit()
        return mem.id


class _RT:
    def __init__(self, sf, user_internal_id=1):
        self._sf = sf
        self.user_internal_id = user_internal_id

    def session_factory(self):
        return self._sf()


async def _collect_memory(sf, *, uid=1, workspace_key=None, agent_type=None):
    from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
    from app.context_engine.models.enums import ContextKind
    from app.context_engine.sources.memory import MemorySourceAdapter

    adapter = MemorySourceAdapter(top_k=10)
    request = ContextRequest(user_id=f"usr_{uid}", agent_type=agent_type, call_site="x",
                             workspace_key=workspace_key)
    scope = ContextScope(user_id=f"usr_{uid}", workspace_key=workspace_key, thread_id="t1")
    section = SectionPlan(kind=ContextKind.MEMORY, required=False, budget_tokens=2000)
    return await adapter.collect(request, section, scope, runtime_context=_RT(sf, uid))


async def _resolve_ref(sf, *, user_id=1, memory_public_id: str) -> str | None:
    """Ref Resolver：给定 memory_public_id 尝试恢复正文（应返回 None / tombstone）。"""
    from app.repositories.context_engine_repositories import ContextMemoryRepository

    async with sf() as session:
        repo = ContextMemoryRepository(session)
        mem = await repo.get_by_public_id(memory_public_id, user_id)
        if mem is None:
            return None
        # get_by_public_id 过滤 deleted_at IS NULL → deleted/forgotten 返回 None
        return mem.content


# ── 1. Forgotten Retrieval = 0 ──────────────────────────────────────────


async def test_forgotten_retrieval_zero(sqlite_session_factory):
    """Forgotten 记忆不再被权威检索返回。"""
    now = datetime.now()
    await _seed_memory(sqlite_session_factory, public_id="m_fg", status="active",
                       deleted_at=now, archived_at=now)
    result = await _collect_memory(sqlite_session_factory)
    assert "m_fg" not in {it.source_ref for it in result.items}


# ── 2. Deleted Retrieval = 0 ────────────────────────────────────────────


async def test_deleted_retrieval_zero(sqlite_session_factory):
    """Deleted 记忆不再被权威检索返回。"""
    now = datetime.now()
    await _seed_memory(sqlite_session_factory, public_id="m_dl", status="deleted",
                       deleted_at=now, archived_at=now)
    result = await _collect_memory(sqlite_session_factory)
    assert "m_dl" not in {it.source_ref for it in result.items}


# ── 3. Snapshot Ref Rehydrate = 0 ───────────────────────────────────────


async def test_snapshot_ref_rehydrate_zero(sqlite_session_factory):
    """已删记忆的 Ref 不能恢复正文（Ref Resolver 返回 tombstone/None）。"""
    now = datetime.now()
    await _seed_memory(sqlite_session_factory, public_id="m_ref", status="active")

    # 先经 MemoryService.delete（tombstone + sources 物理删）
    sf = sqlite_session_factory
    async with sf() as session:
        r = await MemoryService(session).delete(user_id=1, memory_public_id="m_ref")
        assert r["status"] == "deleted"
        await session.commit()

    # Ref Resolver：get_by_public_id 过滤 deleted_at → 返回 None（不能恢复正文）
    content = await _resolve_ref(sf, user_id=1, memory_public_id="m_ref")
    assert content is None  # Snapshot Ref Rehydrate = 0


# ── 4. Payload Rehydrate = 0 ────────────────────────────────────────────


async def test_payload_rehydrate_zero(sqlite_session_factory):
    """关联 Payload 删除后，列表/读取不可达（Payload Rehydrate=0）。"""
    from app.models.context_engine import ContextPayload
    from app.repositories.context_engine_repositories import ContextPayloadRepository
    from app.repositories.base import ensure_model_id

    now = datetime.now()
    async with sqlite_session_factory() as session:
        payload = ContextPayload(
            public_id="pay_ref", user_id=1, source_type="memory",
            source_public_id="m_pay", payload_type="full_text",
            storage_backend="fs", storage_key="k1", storage_key_hash="kh1",
            size_bytes=10, sha256="sh1", status="active", created_at=now,
        )
        await ensure_model_id(session, ContextPayload, payload)
        session.add(payload)
        await session.commit()

    # Memory delete → 关联 Payload 软删
    async with sqlite_session_factory() as session:
        await session.execute(
            __import__("sqlalchemy").update(ContextPayload)
            .where(ContextPayload.source_public_id == "m_pay", ContextPayload.deleted_at.is_(None))
            .values(deleted_at=now)
        )
        await session.commit()

    # Payload Repository 列表/获取 → 不可达
    async with sqlite_session_factory() as session:
        repo = ContextPayloadRepository(session)
        got = await repo.get_by_public_id("pay_ref", 1)
        assert got is None  # 过滤 deleted_at IS NULL → Payload Rehydrate=0
        listed = await repo.list_by_owner(1)
        assert "pay_ref" not in {p.public_id for p in listed}


# ── 5. Source Evidence Rehydrate = 0 ────────────────────────────────────


async def test_source_evidence_rehydrate_zero(sqlite_session_factory):
    """evidence_excerpt 清空后不可读回（Source Evidence Rehydrate=0）。"""
    from sqlalchemy import select
    from app.models.context_engine import ContextMemorySource

    await _seed_memory(sqlite_session_factory, public_id="m_ev", status="active",
                       evidence_excerpt="secret evidence excerpt")

    sf = sqlite_session_factory
    # forget → evidence_excerpt 清空
    async with sf() as session:
        r = await MemoryService(session).forget(user_id=1, memory_public_id="m_ev")
        assert r["status"] == "forgotten"
        await session.commit()

    # 直接查库：evidence_excerpt 已清空，secret 不可读回
    async with sf() as session:
        rows = (await session.execute(select(ContextMemorySource))).scalars().all()
        assert len(rows) >= 1
        for row in rows:
            assert row.evidence_excerpt == "" or row.evidence_excerpt is None
            assert "secret" not in (row.evidence_excerpt or "")
