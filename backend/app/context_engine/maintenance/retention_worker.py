"""CE-05 Retention Worker — MySQL Advisory Lock 单写者清理。

计划 §七：唯一方案 MySQL Advisory Lock。
- 独立数据库连接（专用，不与业务共用池）
- SELECT GET_LOCK('testagent:context-retention', 0) → 返回 1 才执行
- 每阶段确认 IS_USED_LOCK == CONNECTION_ID（硬断言）
- RELEASE_LOCK 返回值检查；锁连接断开自动释放
- 未获取锁 → 本轮退出；不退化本地锁；不并发运行
- dry-run 默认 true（CONTEXT_RETENTION_DRY_RUN）；只统计不删
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

LOCK_NAME = "testagent:context-retention"
DRY_RUN_DEFAULT = True
DEFAULT_INTERVAL_SECONDS = 3600.0


class RetentionLockError(Exception):
    """Advisory Lock 获取/持有校验失败。"""


class RetentionWorker:
    """Retention/Cleanup 单写者 Worker（MySQL Advisory Lock 守护）。"""

    def __init__(
        self,
        *,
        session_factory,
        lock_session_factory=None,
        dry_run: bool | None = None,
        batch_size: int = 1000,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        payload_storage_factory=None,
        event_retention_policy_factory=None,
    ) -> None:
        self._session_factory = session_factory
        self._lock_session_factory = lock_session_factory or session_factory
        self._dry_run = (
            dry_run if dry_run is not None else self._env_dry_run()
        )
        self._batch_size = max(1, min(batch_size, 1000))
        self._interval_seconds = max(0.01, float(interval_seconds))
        self._payload_storage_factory = payload_storage_factory
        self._event_retention_policy_factory = event_retention_policy_factory
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._started_at: datetime | None = None
        self._last_cycle_started_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error_at: datetime | None = None
        self._last_error_code: str | None = None
        self._cycles_total = 0
        self._cycle_errors_total = 0
        self._last_cycle_stats: dict[str, Any] | None = None

    @staticmethod
    def _env_dry_run() -> bool:
        value = os.environ.get("CONTEXT_RETENTION_DRY_RUN", "").strip().lower()
        if value in {"0", "false", "no", "off"}:
            return False
        return True

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    async def start(self) -> None:
        """Start the periodic lifecycle task once for this process."""
        if self._task is not None:
            return
        self._stop_event.clear()
        self._started_at = _utcnow()
        self._task = asyncio.create_task(self._run(), name="context-retention-worker")
        logger.info(
            "Retention worker started | interval=%.1fs | dry_run=%s",
            self._interval_seconds,
            self._dry_run,
        )

    async def stop(self) -> None:
        """Stop the lifecycle task and wait for its cancellation to settle."""
        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        logger.info("Retention worker stopped")

    def health_snapshot(self) -> dict[str, Any]:
        """Return safe lifecycle information; cleanup results contain no content."""
        return {
            "running": self._task is not None and not self._task.done(),
            "dry_run": self._dry_run,
            "interval_seconds": self._interval_seconds,
            "started_at": _iso(self._started_at),
            "last_cycle_started_at": _iso(self._last_cycle_started_at),
            "last_success_at": _iso(self._last_success_at),
            "last_error_at": _iso(self._last_error_at),
            "last_error_code": self._last_error_code,
            "cycles_total": self._cycles_total,
            "cycle_errors_total": self._cycle_errors_total,
            "last_cycle_degraded": bool((self._last_cycle_stats or {}).get("degraded")),
        }

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._last_cycle_started_at = _utcnow()
                stats = await self.run_once()
                self._last_success_at = _utcnow()
                self._last_error_code = None
                self._cycles_total += 1
                self._last_cycle_stats = _safe_stats(stats)
            except RetentionLockError:
                logger.info("Retention skipped; advisory lock held by another worker")
                self._last_error_code = "context.retention.lock_unavailable"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Retention worker iteration failed | err=%s", exc)
                self._last_error_at = _utcnow()
                self._last_error_code = f"context.retention.cycle.{type(exc).__name__}"
                self._cycle_errors_total += 1
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> dict[str, Any]:
        """执行一轮清理（获取 Advisory Lock；未获取 → 退出）。"""
        async with self._lock_session_factory() as lock_session:
            acquired = False
            try:
                # GET_LOCK is connection-bound, so this context spans cleanup and release.
                await self._acquire_lock(lock_session)
                acquired = True
                stats = await self._run_cleanup_cycle(lock_session=lock_session)
                stats["dry_run"] = self._dry_run
                stats["lock_name"] = LOCK_NAME
                return stats
            finally:
                if acquired:
                    await self._release_lock(lock_session)

    async def _acquire_lock(self, session) -> None:
        from sqlalchemy import text

        result = await session.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": LOCK_NAME})
        got = int(result.scalar_one() or 0)
        if got != 1:
            logger.warning("Retention: 未获取 advisory lock %s（另一 Worker 运行中）", LOCK_NAME)
            raise RetentionLockError(f"未获取锁 {LOCK_NAME}")
        # 记录持锁连接 id（校验用）
        conn_id = (await session.execute(text("SELECT CONNECTION_ID()"))).scalar_one()
        session.info["retention_conn_id"] = int(conn_id)
        logger.info("Retention: 获取 advisory lock %s | conn=%s", LOCK_NAME, conn_id)

    async def _assert_lock_owned(self, session) -> None:
        """硬断言 IS_USED_LOCK == CONNECTION_ID；不等 → 停止。"""
        from sqlalchemy import text

        used = (await session.execute(text("SELECT IS_USED_LOCK(:name)"), {"name": LOCK_NAME})).scalar_one()
        conn_id = (await session.execute(text("SELECT CONNECTION_ID()"))).scalar_one()
        if int(used or 0) != int(conn_id):
            raise RetentionLockError(
                f"锁所有权校验失败: IS_USED_LOCK={used} != CONNECTION_ID={conn_id}"
            )

    async def _release_lock(self, session) -> None:
        from sqlalchemy import text

        result = await session.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": LOCK_NAME})
        released = int(result.scalar_one() or 0)
        if released != 1:
            raise RetentionLockError(
                f"释放锁失败: RELEASE_LOCK({LOCK_NAME})={released}"
            )
        logger.info("Retention: 释放 advisory lock %s", LOCK_NAME)

    async def _run_cleanup_cycle(self, *, lock_session) -> dict[str, Any]:
        """按顺序清理（每阶段独立事务 + batch 限制）。"""
        stats = {
            "expired_payloads": 0,
            "orphan_payloads": 0,
            "superseded_summaries": 0,
            "event_rows": 0,
            "index_lease_recovered": 0,
            "payload_failures": 0,
            "stage_failures": [],
        }
        # 1. Payload GC（expired + orphan；复用 CompactionGcService）
        await self._assert_lock_owned(lock_session)
        try:
            from app.context_engine.compression.audit import CompactionGcService
            from app.context_engine.payload.gc_storage import ProductionPayloadGcStorage

            storage_factory = self._payload_storage_factory or ProductionPayloadGcStorage
            gc = CompactionGcService(
                session_factory=self._session_factory,
                payload_storage=storage_factory(
                    session_factory=self._session_factory,
                    batch_size=self._batch_size,
                ),
            )
            if not self._dry_run:
                gc_stats = await gc.run_gc()
                stats["expired_payloads"] = int(gc_stats.get("expired_cleaned", 0))
                stats["orphan_payloads"] = int(gc_stats.get("orphan_cleaned", 0))
                stats["payload_failures"] = int(gc_stats.get("payload_failures", 0))
            else:
                gc_stats = await gc.preview_gc()
                stats["expired_payloads"] = int(gc_stats.get("expired_candidates", 0))
                stats["orphan_payloads"] = int(
                    gc_stats.get("orphan_candidates", 0)
                ) + int(gc_stats.get("compaction_runs_failed_candidates", 0))
                stats["payload_failures"] = int(gc_stats.get("payload_failures", 0))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Retention payload gc 失败 | err=%s", exc)
            stats["stage_failures"].append("payload_gc")

        # 2. Event retention（复用 EventRetentionPolicy；dry-run 只统计）
        await self._assert_lock_owned(lock_session)
        try:
            if self._event_retention_policy_factory is None:
                raise RuntimeError("event retention policy factory is not configured")
            policy = self._event_retention_policy_factory(
                session_factory=self._session_factory
            )
            if not self._dry_run:
                ev = await policy.cleanup_once()
                stats["event_rows"] = int(ev.get("total_deleted", 0))
            else:
                ev = await policy.preview_once()
                stats["event_rows"] = int(ev.get("total_candidates", 0))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Retention event 清理失败 | err=%s", exc)
            stats["stage_failures"].append("event_retention")

        # 3. Superseded summary 淘汰（30d；dry-run 保护）
        await self._assert_lock_owned(lock_session)
        if not self._dry_run:
            try:
                from datetime import datetime, timedelta, timezone
                from sqlalchemy import text

                async with self._session_factory() as session:
                    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
                    result = await session.execute(
                        text(
                            "UPDATE conversation_summaries SET status='archived' "
                            "WHERE status='superseded' AND updated_at < :cutoff "
                            "LIMIT :batch"
                        ),
                        {"cutoff": cutoff, "batch": self._batch_size},
                    )
                    stats["superseded_summaries"] = int(result.rowcount or 0)
                    await session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Retention summary 淘汰失败 | err=%s", exc)
                stats["stage_failures"].append("summary_retention")
        else:
            try:
                from datetime import datetime, timedelta, timezone
                from sqlalchemy import text

                async with self._session_factory() as session:
                    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
                    result = await session.execute(
                        text(
                            "SELECT COUNT(*) FROM conversation_summaries "
                            "WHERE status='superseded' AND updated_at < :cutoff"
                        ),
                        {"cutoff": cutoff},
                    )
                    stats["superseded_summaries"] = min(
                        int(result.scalar() or 0), self._batch_size
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Retention summary dry-run 统计失败 | err=%s", exc)
                stats["stage_failures"].append("summary_retention_preview")

        # 4. Index 过期 lease 恢复（claimed_until 过期且非 active）
        await self._assert_lock_owned(lock_session)
        if not self._dry_run:
            try:
                from datetime import datetime, timezone
                from sqlalchemy import text

                async with self._session_factory() as session:
                    now = datetime.now(timezone.utc)
                    result = await session.execute(
                        text(
                            "UPDATE context_index_jobs SET status='pending', claimed_by=NULL "
                            "WHERE status='claimed' AND claimed_until IS NOT NULL AND claimed_until < :now "
                            "LIMIT :batch"
                        ),
                        {"now": now, "batch": self._batch_size},
                    )
                    stats["index_lease_recovered"] = int(result.rowcount or 0)
                    await session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Retention index lease 恢复失败 | err=%s", exc)
                stats["stage_failures"].append("index_lease_recovery")
        else:
            try:
                from datetime import datetime, timezone
                from sqlalchemy import text

                async with self._session_factory() as session:
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    result = await session.execute(
                        text(
                            "SELECT COUNT(*) FROM context_index_jobs "
                            "WHERE status='claimed' AND claimed_until IS NOT NULL "
                            "AND claimed_until < :now"
                        ),
                        {"now": now},
                    )
                    stats["index_lease_recovered"] = min(
                        int(result.scalar() or 0), self._batch_size
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Retention index lease dry-run 统计失败 | err=%s", exc)
                stats["stage_failures"].append("index_lease_preview")

        stats["degraded"] = bool(stats["stage_failures"] or stats["payload_failures"])

        return stats


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _safe_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded operational counters in process memory."""
    keys = (
        "expired_payloads",
        "orphan_payloads",
        "superseded_summaries",
        "event_rows",
        "index_lease_recovered",
        "payload_failures",
        "degraded",
        "dry_run",
    )
    return {key: stats[key] for key in keys if key in stats}


__all__ = ["RetentionWorker", "RetentionLockError", "LOCK_NAME", "DRY_RUN_DEFAULT"]
# auto-appended module-level note: retention worker: 周期清理过期 ContextPayload / IndexJob。
