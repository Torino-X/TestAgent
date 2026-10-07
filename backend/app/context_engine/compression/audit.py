"""Compaction 审计 + Payload GC（CE-04 WP-8）。

- ``CompactionAuditService``：查询 compaction run 审计（不记正文，只读字段）。
- ``CompactionGcService``：orphan_pending_gc + 过期 payload 清理（复用
  PayloadStorageService.cleanup_expired / cleanup_orphans）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.context_engine import ContextCompactionRun, ContextPayload

logger = logging.getLogger(__name__)

_GC_TTL_DAYS = 7


class CompactionAuditService:
    """Compaction run 审计读取（只读字段，不返回正文）。"""

    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory

    async def list_runs(
        self,
        user_id: int,
        *,
        limit: int = 20,
        status: str | None = None,
        compaction_type: str | None = None,
        conversation_internal_id: int | None = None,
    ) -> list[ContextCompactionRun]:
        """列出用户的 compaction runs（owner-scope）。"""
        if self._session_factory is None:
            return []
        async with self._session_factory() as session:
            stmt = select(ContextCompactionRun).where(ContextCompactionRun.user_id == user_id)
            if status:
                stmt = stmt.where(ContextCompactionRun.status == status)
            if compaction_type:
                stmt = stmt.where(ContextCompactionRun.compaction_type == compaction_type)
            if conversation_internal_id is not None:
                stmt = stmt.where(ContextCompactionRun.conversation_id == conversation_internal_id)
            stmt = stmt.order_by(ContextCompactionRun.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_run(self, user_id: int, run_public_id: str) -> ContextCompactionRun | None:
        if self._session_factory is None:
            return None
        async with self._session_factory() as session:
            result = await session.execute(
                select(ContextCompactionRun).where(
                    ContextCompactionRun.user_id == user_id,
                    ContextCompactionRun.public_id == run_public_id,
                )
            )
            return result.scalar_one_or_none()


class CompactionGcService:
    """Payload GC：orphan_pending_gc + 过期 payload 清理。"""

    def __init__(self, session_factory=None, payload_storage=None) -> None:
        self._session_factory = session_factory
        self._payload_storage = payload_storage

    async def preview_gc(self, *, ttl_days: int = _GC_TTL_DAYS) -> dict[str, int]:
        """Return cleanup candidates without mutating database or blob state."""
        stats = {
            "expired_candidates": 0,
            "orphan_candidates": 0,
            "compaction_runs_failed_candidates": 0,
            "payload_failures": 0,
        }
        if self._session_factory is not None:
            async with self._session_factory() as session:
                cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
                failed_runs = (
                    await session.execute(
                        select(ContextCompactionRun).where(
                            ContextCompactionRun.status == "failed",
                            ContextCompactionRun.created_at < cutoff,
                            ContextCompactionRun.recovery_payload_id.is_not(None),
                        )
                    )
                ).scalars().all()
                stats["compaction_runs_failed_candidates"] = len(failed_runs)
        if self._payload_storage is not None:
            preview = getattr(self._payload_storage, "preview_cleanup", None)
            if callable(preview):
                payload_stats = await preview()
                stats["expired_candidates"] = int(
                    payload_stats.get("expired_candidates", 0)
                )
                stats["orphan_candidates"] = int(
                    payload_stats.get("orphan_candidates", 0)
                )
                stats["payload_failures"] = int(
                    getattr(self._payload_storage, "last_failures", 0)
                )
        return stats

    async def run_gc(self, *, ttl_days: int = _GC_TTL_DAYS) -> dict[str, int]:
        """执行一次 GC：清理 expired / orphan_pending_gc payload。

        返回统计：expired_cleaned / orphan_cleaned / compaction_runs_failed。
        """
        stats = {"expired_cleaned": 0, "orphan_cleaned": 0, "compaction_runs_failed": 0}

        # 1. compaction run 失败且无引用 24h → payload 标 orphan_pending_gc
        if self._session_factory is not None:
            async with self._session_factory() as session:
                cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
                failed_runs = (await session.execute(
                    select(ContextCompactionRun).where(
                        ContextCompactionRun.status == "failed",
                        ContextCompactionRun.created_at < cutoff,
                    )
                )).scalars().all()
                for run in failed_runs:
                    if run.recovery_payload_id:
                        payload = await session.get(ContextPayload, run.recovery_payload_id)
                        if payload is not None and payload.status != "orphan_pending_gc":
                            payload.status = "orphan_pending_gc"
                            stats["compaction_runs_failed"] += 1
                await session.commit()

        # 2. 过期 payload 清理
        if self._payload_storage is not None:
            try:
                cleaned = await self._payload_storage.cleanup_expired()
                stats["expired_cleaned"] = int(cleaned or 0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("compaction gc expired cleanup failed | error=%s", type(exc).__name__)
            try:
                db_orphans, file_orphans = await self._payload_storage.cleanup_orphans()
                stats["orphan_cleaned"] = int(db_orphans) + int(file_orphans)
            except Exception as exc:  # noqa: BLE001
                logger.warning("compaction gc orphan cleanup failed | error=%s", type(exc).__name__)

        stats["payload_failures"] = int(
            getattr(self._payload_storage, "last_failures", 0)
            if self._payload_storage is not None
            else 0
        )

        return stats


__all__ = ["CompactionAuditService", "CompactionGcService"]
# auto-appended module-level note: 压缩审计: 压缩事件落库(谁/何时/before token/after token)。
# auto-appended module-level note: 压缩审计: 压缩事件落库(谁/何时/before token/after token)。
