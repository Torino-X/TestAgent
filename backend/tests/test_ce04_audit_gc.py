"""CE-04 WP-8：降级 + 审计 + Payload GC 测试。

覆盖：
- CompactionAuditService：owner-scope 查询（跨 user 拒绝）、status 过滤。
- CompactionGcService：失败 run 的 payload → orphan_pending_gc；过期清理。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.context_engine.compression.audit import CompactionAuditService, CompactionGcService
from app.models.context_engine import ContextCompactionRun, ContextPayload
from app.repositories.base import ensure_model_id


def _run(coro):
    return asyncio.run(coro)


def _seed_run(sf, *, user_id=1, public_id="run_1", status="failed", created_hours_ago=30):
    async def _seed():
        async with sf() as s:
            run = ContextCompactionRun(
                public_id=public_id,
                user_id=user_id,
                call_site="compression.conversation",
                compaction_type="conversation",
                trigger_type="preflight",
                policy_key="k",
                policy_version="v1",
                tokens_before=100,
                target_tokens=50,
                protected_anchors_json={},
                recovery_mode="summary_with_refs",
                status=status,
                created_at=datetime.now(timezone.utc) - timedelta(hours=created_hours_ago),
            )
            await ensure_model_id(s, ContextCompactionRun, run)
            s.add(run)
            await s.commit()
            return run.id
    return _run(_seed())


def _seed_payload(sf, *, user_id=1, payload_type="recovery_manifest", status="active", expires_hours_ago=2):
    async def _seed():
        async with sf() as s:
            p = ContextPayload(
                public_id="p_" + payload_type,
                user_id=user_id,
                source_type="context_compaction",
                payload_type=payload_type,
                storage_backend="inline",
                storage_key="key1",
                storage_key_hash="h" * 64,
                size_bytes=10,
                sha256="s" * 64,
                status=status,
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) - timedelta(hours=expires_hours_ago),
            )
            await ensure_model_id(s, ContextPayload, p)
            s.add(p)
            await s.commit()
            return p.id
    return _run(_seed())


class TestCompactionAuditService:
    def test_list_owner_scoped(self, sqlite_session_factory):
        sf = sqlite_session_factory
        _seed_run(sf, user_id=1, public_id="run_u1")
        _seed_run(sf, user_id=2, public_id="run_u2")
        svc = CompactionAuditService(session_factory=sf)
        runs = _run(svc.list_runs(user_id=1))
        assert len(runs) == 1
        assert runs[0].public_id == "run_u1"

    def test_filter_by_status(self, sqlite_session_factory):
        sf = sqlite_session_factory
        _seed_run(sf, status="failed")
        _seed_run(sf, public_id="run_2", status="completed")
        svc = CompactionAuditService(session_factory=sf)
        failed = _run(svc.list_runs(user_id=1, status="failed"))
        assert len(failed) == 1
        assert failed[0].status == "failed"

    def test_get_run_owner_scoped(self, sqlite_session_factory):
        sf = sqlite_session_factory
        _seed_run(sf, user_id=1, public_id="run_1")
        svc = CompactionAuditService(session_factory=sf)
        run = _run(svc.get_run(user_id=1, run_public_id="run_1"))
        assert run is not None
        # 跨 user → None
        assert _run(svc.get_run(user_id=2, run_public_id="run_1")) is None


class TestCompactionGcService:
    def test_failed_run_payload_marked_orphan(self, sqlite_session_factory):
        sf = sqlite_session_factory
        _seed_run(sf, public_id="run_1", status="failed", created_hours_ago=30)
        pid = _seed_payload(sf)
        # 关联 payload 到 failed run
        async def _link():
            async with sf() as s:
                run = (await s.execute(
                    select(ContextCompactionRun).where(ContextCompactionRun.public_id == "run_1")
                )).scalar_one()
                run.recovery_payload_id = pid
                await s.commit()
        _run(_link())
        gc = CompactionGcService(session_factory=sf)
        stats = _run(gc.run_gc())
        assert stats["compaction_runs_failed"] >= 1

        async def _check():
            async with sf() as s:
                p = await s.get(ContextPayload, pid)
                assert p.status == "orphan_pending_gc"
        _run(_check())

    def test_expired_payload_cleanup(self, sqlite_session_factory):
        sf = sqlite_session_factory
        _seed_payload(sf, payload_type="recovery_manifest", expires_hours_ago=2)

        class FakeStorage:
            async def cleanup_expired(self):
                return 1

            async def cleanup_orphans(self):
                return 0, 0

        gc = CompactionGcService(session_factory=sf, payload_storage=FakeStorage())
        stats = _run(gc.run_gc())
        assert stats["expired_cleaned"] == 1
