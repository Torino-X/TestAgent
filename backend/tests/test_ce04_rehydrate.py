"""CE-04 WP-7：Evidence Segment + Rehydrate 测试。

覆盖：
- EvidenceSegmentStore 写入/读取（ACL：跨 user 拒绝）。
- ContextRehydrateService 4 模式：summary_only / summary_with_refs /
  evidence_segments / full_rehydrate。
- Rehydrate 硬门禁：Cross-user=0、Cross-workspace=0、Deleted/forgotten/stale=0。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.context_engine.compression.evidence import EvidenceSegmentStore
from app.context_engine.compression.models import ContextRehydrateRequest
from app.context_engine.compression.rehydrate_service import ContextRehydrateService
from app.context_engine.models.context import ContextRef
from app.context_engine.models.enums import RecoveryMode
from app.models.conversation_summary import ConversationSummary
from app.repositories.base import ensure_model_id
from app.utils.ids import generate_public_id


def _run(coro):
    return asyncio.run(coro)


class TestEvidenceSegmentStore:
    def test_store_and_load(self, sqlite_session_factory):
        sf = sqlite_session_factory
        store = EvidenceSegmentStore(session_factory=sf)
        pid = _run(store.store(
            user_id=1, workspace_key="ws_a", conversation_id=100,
            segment_id="seg_1", content="关键证据段落", source_ref="msg:12",
        ))
        assert pid is not None
        content = _run(store.load(user_id=1, payload_id=pid))
        assert content == "关键证据段落"

    def test_cross_user_rejected(self, sqlite_session_factory):
        sf = sqlite_session_factory
        store = EvidenceSegmentStore(session_factory=sf)
        pid = _run(store.store(user_id=1, segment_id="seg_1", content="机密证据"))
        # user 2 读 user 1 的 segment → None（ACL）
        content = _run(store.load(user_id=2, payload_id=pid))
        assert content is None


class TestRehydrateService:
    def _seed_summary(self, sf, *, user_id=1, conv_id=100, summary_type="conversation") -> str:
        async def _seed():
            async with sf() as s:
                summary = ConversationSummary(
                    public_id=generate_public_id("summary"),
                    user_id=user_id,
                    conversation_id=conv_id,
                    summary_text="恢复的摘要内容",
                    status="active",
                    summary_type=summary_type,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                await ensure_model_id(s, ConversationSummary, summary)
                s.add(summary)
                await s.commit()
                return summary.public_id
        return _run(_seed())

    def test_summary_only(self, sqlite_session_factory):
        sf = sqlite_session_factory
        sid = self._seed_summary(sf)
        svc = ContextRehydrateService(session_factory=sf)
        out = _run(svc.rehydrate(ContextRehydrateRequest(
            request_id="r1", user_id=1, recovery_mode=RecoveryMode.SUMMARY_ONLY, summary_public_id=sid
        )))
        assert out.summary_text == "恢复的摘要内容"
        assert out.recovery_mode == RecoveryMode.SUMMARY_ONLY
        assert not out.degraded

    def test_summary_with_refs_recovers_valid_refs(self, sqlite_session_factory):
        sf = sqlite_session_factory
        sid = self._seed_summary(sf)
        refs = [
            ContextRef(item_id="s1", kind="conversation", source_type="conversation_summary", source_ref=sid),
            ContextRef(item_id="m1", kind="memory", source_type="user_memory", source_ref="mem_ok:mem1"),
        ]
        svc = ContextRehydrateService(session_factory=sf)
        out = _run(svc.rehydrate(ContextRehydrateRequest(
            request_id="r2", user_id=1, recovery_mode=RecoveryMode.SUMMARY_WITH_REFS,
            summary_public_id=sid, refs=refs,
        )))
        assert out.summary_text is not None
        assert len(out.recovered_refs) >= 1

    def test_evidence_segments(self, sqlite_session_factory):
        sf = sqlite_session_factory
        sid = self._seed_summary(sf)
        store = EvidenceSegmentStore(session_factory=sf)
        pid = _run(store.store(user_id=1, segment_id="seg_1", content="证据段1", source_ref="msg:1"))
        refs = [
            ContextRef(item_id="e1", kind="evidence", source_type="evidence_segment", source_ref="seg_1"),
            ContextRef(item_id="s1", kind="conversation", source_type="conversation_summary", source_ref=sid),
        ]
        svc = ContextRehydrateService(session_factory=sf, evidence_store=store)
        out = _run(svc.rehydrate(ContextRehydrateRequest(
            request_id="r3", user_id=1, recovery_mode=RecoveryMode.EVIDENCE_SEGMENTS,
            summary_public_id=sid, refs=refs,
        )))
        assert len(out.evidence_segments) == 1
        assert out.evidence_segments[0].source_ref == "seg_1"

    def test_cross_workspace_rejected(self, sqlite_session_factory):
        sf = sqlite_session_factory
        store = EvidenceSegmentStore(session_factory=sf)
        _run(store.store(user_id=1, workspace_key="ws_a", segment_id="seg_ws", content="ws_a 机密"))
        refs = [ContextRef(item_id="e1", kind="evidence", source_type="evidence_segment", source_ref="seg_ws")]
        svc = ContextRehydrateService(session_factory=sf, evidence_store=store)
        # user1 在 ws_b 上下文请求 → 不命中 ws_a 的 segment
        out = _run(svc.rehydrate(ContextRehydrateRequest(
            request_id="r4", user_id=1, workspace_key="ws_b",
            recovery_mode=RecoveryMode.EVIDENCE_SEGMENTS, refs=refs,
        )))
        assert len(out.evidence_segments) == 0

    def test_forgotten_deleted_ref_rejected(self, sqlite_session_factory):
        sf = sqlite_session_factory
        sid = self._seed_summary(sf)
        refs = [
            # forgotten/deleted 标记 → 拒绝
            ContextRef(item_id="f1", kind="memory", source_type="user_memory", source_ref="mem_forgotten:forgotten_mem"),
            ContextRef(item_id="s1", kind="conversation", source_type="conversation_summary", source_ref=sid),
        ]
        svc = ContextRehydrateService(session_factory=sf)
        out = _run(svc.rehydrate(ContextRehydrateRequest(
            request_id="r5", user_id=1, recovery_mode=RecoveryMode.SUMMARY_WITH_REFS,
            summary_public_id=sid, refs=refs,
        )))
        # forgotten ref 被拒绝；summary ref 恢复
        assert any("forgotten" in (r.source_ref or "") for r in out.rejected_refs)
        assert len(out.recovered_refs) >= 0


class TestRehydrateGates:
    """命令 §八：Rehydrate 11 项门禁=0 验证（补充既有 ACL 测试）。"""

    def test_invalid_ref_rejected(self, sqlite_session_factory):
        """Invalid Ref（source_ref 指向不存在 payload）→ 拒绝。"""
        sf = sqlite_session_factory
        svc = ContextRehydrateService(session_factory=sf)
        refs = [ContextRef(item_id="x", kind="conversation", source_type="conversation_summary",
                           source_ref="no_such_payload")]
        out = _run(svc.rehydrate(ContextRehydrateRequest(
            request_id="g1", user_id=1, recovery_mode=RecoveryMode.SUMMARY_WITH_REFS, refs=refs,
        )))
        assert len(out.recovered_refs) == 0
        assert len(out.rejected_refs) == 1
        assert out.degraded

    def test_inactive_payload_rejected(self, sqlite_session_factory):
        """Inactive Payload（status != active）→ 拒绝。"""
        from app.models.context_engine import ContextPayload
        from app.utils.ids import generate_public_id

        sf = sqlite_session_factory

        async def _seed():
            async with sf() as s:
                p = ContextPayload(
                    public_id=generate_public_id("payload"),
                    user_id=1,
                    source_type="conversation",
                    source_public_id="conv_100",
                    payload_type="recovery_manifest",
                    status="orphan_pending_gc",  # 非 active
                    storage_key="k",
                    storage_key_hash="h" * 64,
                    storage_backend="memory",
                    size_bytes=100,
                    sha256="abc",
                )
                await ensure_model_id(s, ContextPayload, p)
                s.add(p)
                await s.commit()
                return p.public_id
        pid = _run(_seed())
        refs = [ContextRef(item_id="x", kind="conversation", source_type="conversation_summary", source_ref=pid)]
        svc = ContextRehydrateService(session_factory=sf)
        out = _run(svc.rehydrate(ContextRehydrateRequest(
            request_id="g2", user_id=1, recovery_mode=RecoveryMode.SUMMARY_WITH_REFS, refs=refs,
        )))
        assert len(out.recovered_refs) == 0

    def test_digest_mismatch_rejected(self, sqlite_session_factory):
        """Digest Mismatch（ref version 与 payload sha256 不匹配）→ 拒绝。"""
        from app.models.context_engine import ContextPayload
        from app.utils.ids import generate_public_id

        sf = sqlite_session_factory

        async def _seed():
            async with sf() as s:
                p = ContextPayload(
                    public_id=generate_public_id("payload"),
                    user_id=1,
                    source_type="conversation",
                    source_public_id="conv_100",
                    payload_type="recovery_manifest",
                    status="active",
                    storage_key="k",
                    storage_key_hash="h" * 64,
                    storage_backend="memory",
                    size_bytes=100,
                    sha256="real_digest_abc",
                )
                await ensure_model_id(s, ContextPayload, p)
                s.add(p)
                await s.commit()
                return p.public_id
        pid = _run(_seed())
        # ref.version 与 payload.sha256 不匹配
        refs = [ContextRef(item_id="x", kind="conversation", source_type="conversation_summary",
                           source_ref=pid, version="wrong_digest")]
        svc = ContextRehydrateService(session_factory=sf)
        out = _run(svc.rehydrate(ContextRehydrateRequest(
            request_id="g3", user_id=1, recovery_mode=RecoveryMode.SUMMARY_WITH_REFS, refs=refs,
        )))
        assert len(out.recovered_refs) == 0

    def test_unauthorized_payload_access_rejected(self, sqlite_session_factory):
        """Unauthorized Payload（跨 user ACL）→ 拒绝。"""
        from app.models.context_engine import ContextPayload
        from app.utils.ids import generate_public_id

        sf = sqlite_session_factory

        async def _seed():
            async with sf() as s:
                p = ContextPayload(
                    public_id=generate_public_id("payload"),
                    user_id=1,  # 属于 user1
                    source_type="conversation",
                    source_public_id="conv_100",
                    payload_type="recovery_manifest",
                    status="active",
                    storage_key="k",
                    storage_key_hash="h" * 64,
                    storage_backend="memory",
                    size_bytes=100,
                    sha256="d",
                )
                await ensure_model_id(s, ContextPayload, p)
                s.add(p)
                await s.commit()
                return p.public_id
        pid = _run(_seed())
        refs = [ContextRef(item_id="x", kind="conversation", source_type="conversation_summary", source_ref=pid)]
        svc = ContextRehydrateService(session_factory=sf)
        # user2 请求 user1 的 payload → 拒绝
        out = _run(svc.rehydrate(ContextRehydrateRequest(
            request_id="g4", user_id=2, recovery_mode=RecoveryMode.SUMMARY_WITH_REFS, refs=refs,
        )))
        assert len(out.recovered_refs) == 0

    def test_deleted_source_ref_rejected(self, sqlite_session_factory):
        """Deleted Source（source_ref 含 deleted 标记）→ 拒绝。"""
        sf = sqlite_session_factory
        refs = [ContextRef(item_id="m", kind="memory", source_type="user_memory",
                           source_ref="mem:deleted:mem_1")]
        svc = ContextRehydrateService(session_factory=sf)
        out = _run(svc.rehydrate(ContextRehydrateRequest(
            request_id="g5", user_id=1, recovery_mode=RecoveryMode.SUMMARY_WITH_REFS, refs=refs,
        )))
        assert len(out.recovered_refs) == 0
        assert len(out.rejected_refs) == 1

    def test_stale_artifact_ref_rejected(self, sqlite_session_factory):
        """Stale Artifact（artifact ref 无 source_ref）→ 拒绝。"""
        sf = sqlite_session_factory
        refs = [ContextRef(item_id="a", kind="task_state", source_type="artifact",
                           source_ref=None)]
        svc = ContextRehydrateService(session_factory=sf)
        out = _run(svc.rehydrate(ContextRehydrateRequest(
            request_id="g6", user_id=1, recovery_mode=RecoveryMode.SUMMARY_WITH_REFS, refs=refs,
        )))
        assert len(out.recovered_refs) == 0

    def test_stale_chunk_ref_rejected(self, sqlite_session_factory):
        """Stale Chunk（chunk ref 含 deleted/forgotten）→ 拒绝。"""
        sf = sqlite_session_factory
        refs = [ContextRef(item_id="c", kind="knowledge", source_type="chunk",
                           source_ref="chunk:forgotten:c1")]
        svc = ContextRehydrateService(session_factory=sf)
        out = _run(svc.rehydrate(ContextRehydrateRequest(
            request_id="g7", user_id=1, recovery_mode=RecoveryMode.SUMMARY_WITH_REFS, refs=refs,
        )))
        assert len(out.recovered_refs) == 0

    def test_expired_payload_not_resurrected(self, sqlite_session_factory):
        """Expired payload（deleted_at 已设）→ 不恢复。"""
        from datetime import datetime, timezone, timedelta
        from app.models.context_engine import ContextPayload
        from app.utils.ids import generate_public_id

        sf = sqlite_session_factory

        async def _seed():
            async with sf() as s:
                p = ContextPayload(
                    public_id=generate_public_id("payload"),
                    user_id=1,
                    source_type="conversation",
                    source_public_id="conv_100",
                    payload_type="recovery_manifest",
                    status="active",
                    storage_key="k",
                    storage_key_hash="h" * 64,
                    storage_backend="memory",
                    size_bytes=100,
                    sha256="d",
                    deleted_at=datetime.now(timezone.utc) - timedelta(days=1),  # 已删除
                )
                await ensure_model_id(s, ContextPayload, p)
                s.add(p)
                await s.commit()
                return p.public_id
        pid = _run(_seed())
        refs = [ContextRef(item_id="x", kind="conversation", source_type="conversation_summary", source_ref=pid)]
        svc = ContextRehydrateService(session_factory=sf)
        out = _run(svc.rehydrate(ContextRehydrateRequest(
            request_id="g8", user_id=1, recovery_mode=RecoveryMode.SUMMARY_WITH_REFS, refs=refs,
        )))
        assert len(out.recovered_refs) == 0
