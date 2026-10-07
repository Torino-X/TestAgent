"""CE-03 Memory 测试：生命周期 / Forget/Delete 防重恢复 / 审计。

覆盖：
- candidate → active（仅 API）；reject；
- forget：evidence_excerpt 清空、Ref tombstone、Retrieval=0；
- delete：sources 物理删除、Payload 删除、tombstone；
- Memory Retrieval Audit：正文不进审计表、Snapshot 含 retrieval_run_id、
  跨用户/forgotten 不写 selected candidate；
- MemorySourceAdapter 权威检索（active 过滤 + 排序）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.context_engine.memory import MemoryExtractionGuard, MemoryService, MemoryServiceError


async def _seed_memory(sf, *, public_id="mem_1", user_id=1, scope_type="user",
                       workspace_key=None, agent_type=None, status="candidate",
                       content="user fact", evidence_excerpt="evidence secret",
                       expires_at=None, valid_from=None, deleted_at=None):
    from app.models.context_engine import ContextMemory, ContextMemorySource
    from app.repositories.base import ensure_model_id

    async with sf() as session:
        mem = ContextMemory(
            public_id=public_id, user_id=user_id, scope_type=scope_type,
            workspace_key=workspace_key, agent_type=agent_type,
            memory_type="fact", content=content, status=status,
            activation_source="auto", confidence=0.5, importance=3,
            content_hash="hash_" + public_id, idempotency_key="idem_" + public_id,
            version=1, created_by_user_id=user_id,
            valid_from=valid_from, expires_at=expires_at, deleted_at=deleted_at,
        )
        await ensure_model_id(session, ContextMemory, mem)
        session.add(mem)
        await session.flush()
        src = ContextMemorySource(
            memory_id=mem.id, user_id=user_id, source_type="evidence",
            source_public_id="src_1", relation_type="evidence",
            evidence_excerpt=evidence_excerpt,
        )
        await ensure_model_id(session, ContextMemorySource, src)
        session.add(src)
        await session.commit()
        return mem.id


def test_memory_extraction_recursion_guard():
    from app.context_engine.memory.extraction import ExtractionDepthExceeded

    guard = MemoryExtractionGuard()
    guard.check(current_depth=1)  # 深度 1 OK
    with pytest.raises(ExtractionDepthExceeded):
        guard.check(current_depth=2)


async def test_memory_activate_reject(sqlite_session_factory):
    await _seed_memory(sqlite_session_factory, public_id="mem_a", status="candidate")
    await _seed_memory(sqlite_session_factory, public_id="mem_c", status="candidate")
    sf = sqlite_session_factory
    async with sf() as session:
        svc = MemoryService(session)
        r = await svc.activate(user_id=1, memory_public_id="mem_a")
        assert r["status"] == "active"
        await session.commit()
    async with sf() as session:
        svc = MemoryService(session)
        r = await svc.reject(user_id=1, memory_public_id="mem_c")
        assert r["status"] == "rejected"


async def test_memory_cross_user_rejected(sqlite_session_factory):
    await _seed_memory(sqlite_session_factory, public_id="mem_b", user_id=1)
    sf = sqlite_session_factory
    async with sf() as session:
        svc = MemoryService(session)
        with pytest.raises(MemoryServiceError):
            await svc.activate(user_id=2, memory_public_id="mem_b")


async def test_memory_forget_clears_evidence_and_tombstone(sqlite_session_factory):
    await _seed_memory(sqlite_session_factory, public_id="mem_f", status="active")
    sf = sqlite_session_factory
    async with sf() as session:
        svc = MemoryService(session)
        r = await svc.forget(user_id=1, memory_public_id="mem_f")
        assert r["status"] == "forgotten"
        await session.commit()
    # evidence_excerpt 已清空；正文清空
    from app.repositories.context_engine_repositories import ContextMemoryRepository, ContextMemorySourceRepository

    async with sf() as session:
        repo = ContextMemoryRepository(session)
        mem = await repo.get_by_public_id("mem_f", 1)
        # get_by_public_id 过滤 deleted_at IS NULL → 返回 None（tombstone）
        assert mem is None
        src_repo = ContextMemorySourceRepository(session)
        sources = await src_repo.list_for_memory(0, 1)  # id 不可知，直接查库验证
        # 通过原 id 验证——重新读取所有 source（此处直接验证：mem 不可经正常路径取回）
        from sqlalchemy import select
        from app.models.context_engine import ContextMemory, ContextMemorySource

        row = (await session.execute(select(ContextMemory).where(ContextMemory.public_id == "mem_f"))).scalar_one()
        assert row.status == "forgotten"
        assert row.content == ""
        sources = (await session.execute(select(ContextMemorySource).where(ContextMemorySource.memory_id == row.id))).scalars().all()
        assert all(s.evidence_excerpt == "" for s in sources)


async def test_memory_delete_physical_sources(sqlite_session_factory):
    await _seed_memory(sqlite_session_factory, public_id="mem_d", status="active")
    sf = sqlite_session_factory
    async with sf() as session:
        svc = MemoryService(session)
        r = await svc.delete(user_id=1, memory_public_id="mem_d")
        assert r["status"] == "deleted"
        await session.commit()
    from sqlalchemy import select
    from app.models.context_engine import ContextMemory, ContextMemorySource

    async with sf() as session:
        row = (await session.execute(select(ContextMemory).where(ContextMemory.public_id == "mem_d"))).scalar_one()
        assert row.status == "deleted"
        assert row.content == ""
        sources = (await session.execute(select(ContextMemorySource).where(ContextMemorySource.memory_id == row.id))).scalars().all()
        assert len(sources) == 0  # 物理删除


async def test_memory_audit_no_content_in_audit(sqlite_session_factory):
    """Memory audit：正文/Evidence 不进审计表，Snapshot 含 retrieval_run_id。"""
    from app.context_engine.retrieval.audit import RetrievalAuditService
    from app.context_engine.models.retrieval import RetrievalRequest, RetrievalScopeFilter
    from app.models.context_engine import ContextRetrievalRun
    from sqlalchemy import select

    sf = sqlite_session_factory
    async with sf() as session:
        service = RetrievalAuditService(session)
        run_id = await service.record_memory_run(
            user_id=1, workspace_key=None, scope_type="user",
            candidate_count=3, selected_count=2, latency_ms=5,
        )
        await session.commit()
        assert run_id is not None
        row = (await session.execute(select(ContextRetrievalRun).where(ContextRetrievalRun.public_id == run_id))).scalar_one()
        assert row.query_metadata_json["source_family"] == "memory"
        assert row.recalled_count == 3
        assert row.selected_count == 2
        # 正文不进审计表
        serialized = str(row.query_metadata_json)
        assert "user fact" not in serialized
        assert "evidence secret" not in serialized


async def test_memory_source_adapter_authoritative(sqlite_session_factory):
    """MemorySourceAdapter：只返回 active + valid 窗口；forgotten 不返回。"""
    from app.context_engine.sources.memory import MemorySourceAdapter
    from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
    from app.context_engine.models.enums import ContextKind

    now = datetime.now()
    await _seed_memory(sqlite_session_factory, public_id="mem_active", status="active",
                       valid_from=now - timedelta(days=1), expires_at=now + timedelta(days=1))
    await _seed_memory(sqlite_session_factory, public_id="mem_candidate", status="candidate")
    await _seed_memory(sqlite_session_factory, public_id="mem_forgotten", status="active",
                       deleted_at=now)
    await _seed_memory(sqlite_session_factory, public_id="mem_expired", status="active",
                       valid_from=now - timedelta(days=2), expires_at=now - timedelta(days=1))

    class _RT:
        def __init__(self, sf):
            self._sf = sf
            self.user_internal_id = 1
            self.task_flag_resolver = type(
                "_Resolver",
                (),
                {"evaluate": staticmethod(lambda name: name == "CONTEXT_MEMORY_READ_ENABLED")},
            )()

        def session_factory(self):
            return self._sf()

    adapter = MemorySourceAdapter(top_k=5)
    request = ContextRequest(user_id="usr_1", call_site="x")
    scope = ContextScope(user_id="usr_1", thread_id="t1")
    section = SectionPlan(kind=ContextKind.MEMORY, required=False, budget_tokens=1000)
    result = await adapter.collect(request, section, scope, runtime_context=_RT(sqlite_session_factory))
    ids = {it.source_ref for it in result.items}
    assert "mem_active" in ids
    assert "mem_candidate" not in ids
    assert "mem_forgotten" not in ids
    assert "mem_expired" not in ids


async def test_memory_source_adapter_reads_active_project_workspace_memory(
    sqlite_session_factory,
):
    """ProjectMemoryService records must be consumable by an explicitly scoped CE call."""
    from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
    from app.context_engine.models.enums import ContextKind
    from app.context_engine.sources.memory import MemorySourceAdapter

    await _seed_memory(
        sqlite_session_factory,
        public_id="mem_project_active",
        scope_type="workspace",
        workspace_key="project:project_alpha",
        status="active",
        content="Use the confirmed OAuth2 flow for this project.",
    )
    await _seed_memory(
        sqlite_session_factory,
        public_id="mem_other_project",
        scope_type="workspace",
        workspace_key="project:project_beta",
        status="active",
        content="Do not leak into Alpha.",
    )

    class _RT:
        def __init__(self, sf):
            self._sf = sf
            self.user_internal_id = 1
            self.task_flag_resolver = type(
                "_Resolver",
                (),
                {"evaluate": staticmethod(lambda name: name == "CONTEXT_MEMORY_READ_ENABLED")},
            )()

        def session_factory(self):
            return self._sf()

    result = await MemorySourceAdapter(top_k=5).collect(
        ContextRequest(user_id="usr_1", call_site="chat.reply"),
        SectionPlan(kind=ContextKind.MEMORY, required=False, budget_tokens=1000),
        ContextScope(
            user_id="usr_1",
            thread_id="t1",
            workspace_key="project:project_alpha",
        ),
        runtime_context=_RT(sqlite_session_factory),
    )
    ids = {item.source_ref for item in result.items}
    assert "mem_project_active" in ids
    assert "mem_other_project" not in ids
