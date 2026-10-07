"""Cache maintenance service — owns low-frequency background cleanups.

Phase 0 extraction: the library "purge expired deleted" sweep used to
run inside every ``GET /api/library/items`` call.  That mixed a bulk
DELETE workload with a read hot path and inflated every list request
by an unbounded amount of work.  This service owns the sweep and is
driven from:

1. The AgentExecutionWorker idle branch (every ``poll_interval``
   seconds when no outbox row is claimed) — opportunistic, in-process.
2. A dedicated ``CacheMaintenanceWorker`` background task started in
   ``main.py`` lifespan every ``cache_maintenance_interval_seconds``
   seconds — safety-net, separate asyncio task.

Both entrypoints share the same single-shot ``run_once()`` so behaviour
is identical regardless of trigger.  The implementation is fail-soft:
exceptions inside ``run_once`` are logged but do not propagate, so the
caller never loses its primary duty (outbox polling / heartbeat).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.file_repository import FileRepository
from app.storage.oss_storage import object_storage
from app.utils.datetime import utcnow

logger = logging.getLogger(__name__)


# Items deleted more than this long ago are eligible for hard delete.
DEFAULT_PURGE_RETENTION_DAYS = 30


class CacheMaintenanceService:
    """Owns low-frequency background maintenance work (library purge, etc.).

    Stateless w.r.t. session: each call opens a fresh ``AsyncSessionLocal``
    session so the worker does not have to coordinate transactions with
    the AgentExecutionWorker's own session lifecycle.
    """

    def __init__(self) -> None:
        self._last_run_at: float | None = None

    async def run_once(self) -> int:
        """Run all registered maintenance jobs once.

        Returns total number of items purged (across all jobs).  Failures
        inside an individual job are logged and swallowed so a single
        broken job does not block the others.
        """
        purged = 0
        for job in (
            self.purge_expired_deleted_library,
        ):
            try:
                purged += await job()
            except Exception as exc:  # noqa: BLE001 — fail-soft
                logger.warning(
                    "CacheMaintenanceService: job %s failed | error=%s",
                    getattr(job, "__name__", repr(job)),
                    exc,
                )
        self._last_run_at = asyncio.get_event_loop().time()
        return purged

    async def purge_expired_deleted_library(self) -> int:
        """Hard-delete UploadedFile / Artifact rows older than retention.

        Replaces ``LibraryService._purge_expired_deleted`` which previously
        ran synchronously on every GET.  Paginates by user_id slices so a
        single user with a large purge backlog does not block the worker.
        Intended to run from a background asyncio task; safe to run
        concurrently with user library reads (each session commits
        independently).
        """
        settings = get_settings()
        retention_days = DEFAULT_PURGE_RETENTION_DAYS
        cutoff = utcnow() - timedelta(days=retention_days)

        purged_total = 0
        async with AsyncSessionLocal() as session:
            file_repo = FileRepository(session)
            artifact_repo = ArtifactRepository(session)
            # Find user_ids that have expired soft-deleted items.  Use a
            # bounded DISTINCT scan to avoid loading every expired row
            # into memory; the per-user fetch is bounded by list_*_by_user
            # ``limit`` (200) so worst case we sweep 200 items per user
            # per run, picking up the rest on subsequent ticks.
            from sqlalchemy import distinct, select

            from app.models.uploaded_file import UploadedFile as UFModel
            from app.models.artifact import Artifact as ArtModel

            user_ids_with_files = (
                await session.execute(
                    select(distinct(UFModel.user_id)).where(
                        UFModel.deleted_at.is_not(None),
                        UFModel.deleted_at <= cutoff,
                    ).limit(500)
                )
            ).scalars().all()
            user_ids_with_artifacts = (
                await session.execute(
                    select(distinct(ArtModel.user_id)).where(
                        ArtModel.deleted_at.is_not(None),
                        ArtModel.deleted_at <= cutoff,
                    ).limit(500)
                )
            ).scalars().all()
            user_ids = set(user_ids_with_files) | set(user_ids_with_artifacts)

        for uid in user_ids:
            try:
                purged_total += await self._purge_one_user(uid, cutoff)
            except Exception as exc:  # noqa: BLE001 — fail-soft
                logger.warning(
                    "CacheMaintenanceService: user=%d purge failed | error=%s",
                    uid, exc,
                )

        if purged_total:
            logger.info(
                "CacheMaintenanceService.purge_expired_deleted_library: "
                "purged=%d users=%d retention_days=%d",
                purged_total, len(user_ids), retention_days,
            )
        return purged_total

    async def _purge_one_user(self, user_id: int, cutoff) -> int:
        async with AsyncSessionLocal() as session:
            file_repo = FileRepository(session)
            artifact_repo = ArtifactRepository(session)
            purged = 0

            uploads = await file_repo.list_deleted_by_user(user_id)
            for f in uploads:
                if f.deleted_at and f.deleted_at <= cutoff:
                    storage_path = getattr(f, "storage_path", "") or ""
                    if storage_path:
                        try:
                            await object_storage.delete_file(storage_path)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(
                                "purge: storage delete skipped user=%d file=%s err=%s",
                                user_id, f.public_id, exc,
                            )
                    await file_repo.hard_delete(f.public_id)
                    purged += 1

            artifacts = await artifact_repo.list_deleted_by_user(user_id)
            for a in artifacts:
                if a.deleted_at and a.deleted_at <= cutoff:
                    storage_path = getattr(a, "storage_path", "") or ""
                    if storage_path:
                        try:
                            await object_storage.delete_file(storage_path)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(
                                "purge: storage delete skipped user=%d artifact=%s err=%s",
                                user_id, a.public_id, exc,
                            )
                    await artifact_repo.hard_delete(a.public_id)
                    purged += 1

            if purged:
                await session.commit()

            # P0 收口:purge 改变了 library list 的可见内容 — 即使低频,任何
            # 改变 list 视图的 mutation 都必须 bump_generation. 否则用户在
            # 30 天后看到已经 hard-delete 的条目仍会出现在 60s TTL 内的 cache
            # 中。cache 失败仅 log,主流程不受影响。
            if purged:
                try:
                    from app.cache.domains.library_cache import (
                        get_library_cache,
                    )
                    await get_library_cache().bump_generation(user_id)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "purge: library bump_generation failed user=%d err=%s",
                        user_id, exc,
                    )
            return purged


# ── Independent background worker (lifespan-managed) ──────────────────────


class CacheMaintenanceWorker:
    """Standalone asyncio task that drives ``CacheMaintenanceService`` on a
    low-frequency cadence.

    Phase 0 decision (per design doc §14.4 + prompt §15.3): the purge
    sweep must not live inside the GET hot path.  Two complementary
    drivers are wired:

    * The AgentExecutionWorker opportunistically calls
      ``CacheMaintenanceService.run_once`` every idle poll tick.
    * This worker runs as a separate asyncio task with its own
      interval (``cache_maintenance_interval_seconds``, default 60s) as
      a safety net even when AgentExecutionWorker is busy or has not
      been started (test / single-purpose deployments).
    """

    DEFAULT_INTERVAL_SECONDS = 60.0

    def __init__(self, *, interval_seconds: float | None = None) -> None:
        settings = get_settings()
        configured = getattr(settings, "cache_maintenance_interval_seconds", None)
        self._interval = float(
            interval_seconds
            if interval_seconds is not None
            else (configured if configured else self.DEFAULT_INTERVAL_SECONDS)
        )
        self._service = CacheMaintenanceService()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="cache-maintenance-worker")
        logger.info(
            "CacheMaintenanceWorker started | interval=%.1fs",
            self._interval,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        logger.info("CacheMaintenanceWorker stopped")

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._service.run_once()
            except Exception as exc:  # noqa: BLE001 — fail-soft
                logger.exception("CacheMaintenanceWorker iteration failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
