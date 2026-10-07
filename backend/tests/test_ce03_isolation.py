"""CE-03 隔离测试：Cross-user / Cross-workspace / Forgotten / Deleted Leakage。

硬门禁：Cross-user Leakage=0 / Cross-workspace Leakage=0 /
Forgotten Retrieval=0 / Deleted Retrieval=0。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind


@pytest.fixture(autouse=True)
def _enable_memory_read_contract(monkeypatch):
    """Isolation tests exercise retrieval, so opt in explicitly."""
    monkeypatch.setenv("CONTEXT_MEMORY_READ_ENABLED", "true")
    monkeypatch.setenv("LEGACY_WORKSPACE_MEMORY_ENABLED", "true")


async def _seed_memory(sf, *, public_id, user_id, scope_type, workspace_key=None,
                       agent_type=None, status="active", deleted_at=None, archived_at=None):
    from app.models.context_engine import ContextMemory
    from app.repositories.base import ensure_model_id

    now = datetime.now()
    async with sf() as session:
        mem = ContextMemory(
            public_id=public_id, user_id=user_id, scope_type=scope_type,
            workspace_key=workspace_key, agent_type=agent_type,
            memory_type="fact", content="secret-" + public_id, status=status,
            activation_source="manual", confidence=0.5, importance=3,
            content_hash="h_" + public_id, idempotency_key="i_" + public_id,
            version=1, created_by_user_id=user_id,
            valid_from=now - timedelta(days=1), expires_at=now + timedelta(days=1),
            deleted_at=deleted_at, archived_at=archived_at,
        )
        await ensure_model_id(session, ContextMemory, mem)
        session.add(mem)
        await session.commit()


class _RT:
    def __init__(self, sf, user_internal_id):
        self._sf = sf
        self.user_internal_id = user_internal_id

    def session_factory(self):
        return self._sf()


async def _collect_memory(sf, *, uid, workspace_key=None, agent_type=None):
    from app.context_engine.sources.memory import MemorySourceAdapter

    adapter = MemorySourceAdapter(top_k=10)
    request = ContextRequest(user_id=f"usr_{uid}", agent_type=agent_type, call_site="x",
                             workspace_key=workspace_key)
    scope = ContextScope(user_id=f"usr_{uid}", workspace_key=workspace_key, thread_id="t1")
    section = SectionPlan(kind=ContextKind.MEMORY, required=False, budget_tokens=2000)
    return await adapter.collect(request, section, scope, runtime_context=_RT(sf, uid))


async def test_cross_user_memory_leakage_zero(sqlite_session_factory):
    """用户 1 的记忆对用户 2 不可见（Cross-user Leakage=0）。"""
    await _seed_memory(sqlite_session_factory, public_id="m_u1", user_id=1,
                       scope_type="user", status="active")
    await _seed_memory(sqlite_session_factory, public_id="m_u2", user_id=2,
                       scope_type="user", status="active")
    result = await _collect_memory(sqlite_session_factory, uid=1)
    ids = {it.source_ref for it in result.items}
    assert "m_u1" in ids
    assert "m_u2" not in ids


async def test_cross_workspace_memory_leakage_zero(sqlite_session_factory):
    """workspace A 记忆对 workspace B 不可见（Cross-workspace Leakage=0）。"""
    await _seed_memory(sqlite_session_factory, public_id="m_wsA", user_id=1,
                       scope_type="workspace", workspace_key="ws_a", status="active")
    await _seed_memory(sqlite_session_factory, public_id="m_wsB", user_id=1,
                       scope_type="workspace", workspace_key="ws_b", status="active")
    result = await _collect_memory(sqlite_session_factory, uid=1, workspace_key="ws_a")
    ids = {it.source_ref for it in result.items}
    assert "m_wsA" in ids
    assert "m_wsB" not in ids


async def test_forgotten_memory_retrieval_zero(sqlite_session_factory):
    """forgotten 记忆 Retrieval=0。"""
    now = datetime.now()
    await _seed_memory(sqlite_session_factory, public_id="m_forgot", user_id=1,
                       scope_type="user", status="active", deleted_at=now)
    result = await _collect_memory(sqlite_session_factory, uid=1)
    assert "m_forgot" not in {it.source_ref for it in result.items}


async def test_deleted_memory_retrieval_zero(sqlite_session_factory):
    """deleted 记忆 Retrieval=0。"""
    now = datetime.now()
    await _seed_memory(sqlite_session_factory, public_id="m_del", user_id=1,
                       scope_type="user", status="deleted", deleted_at=now, archived_at=now)
    result = await _collect_memory(sqlite_session_factory, uid=1)
    assert "m_del" not in {it.source_ref for it in result.items}


async def test_playbook_wrong_agent_leakage_zero(sqlite_session_factory):
    """Playbook 记忆：agent_type 不匹配 → Leakage=0。"""
    await _seed_memory(sqlite_session_factory, public_id="m_pb", user_id=1,
                       scope_type="agent_playbook", agent_type="prep", status="active")
    result = await _collect_memory(sqlite_session_factory, uid=1, agent_type="review")
    assert "m_pb" not in {it.source_ref for it in result.items}
    result_ok = await _collect_memory(sqlite_session_factory, uid=1, agent_type="prep")
    assert "m_pb" in {it.source_ref for it in result_ok.items}


async def test_memory_scope_filter_matches_db_row():
    """RetrievalScopeFilter 与 Memory repo 语义一致（Playbook 全条件）。"""
    from app.context_engine.models.retrieval import RetrievalScopeFilter

    f = RetrievalScopeFilter(mode="playbook", user_id=1, agent_type="prep")
    assert f.matches_row(
        owner_user_id=1, owner_workspace_key=None,
        row_scope_type="agent_playbook", row_agent_type="prep",
    )
    # 跨用户 → False
    assert not f.matches_row(
        owner_user_id=2, owner_workspace_key=None,
        row_scope_type="agent_playbook", row_agent_type="prep",
    )
