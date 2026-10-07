"""Database/blob-consistent production payload cleanup coordinator."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.context_engine.payload.factory import build_external_payload_backends
from app.models.context_engine import ContextPayload


class ProductionPayloadGcStorage:
    """Clean payload rows by their persisted backend without cross-backend guesses."""

    def __init__(
        self,
        *,
        session_factory,
        backends: dict[str, Any] | None = None,
        batch_size: int = 1000,
    ) -> None:
        self._session_factory = session_factory
        # Lazy construction is required: dry-run must not even create the
        # historical filesystem root as a side effect.
        self._backends = backends
        self._batch_size = max(1, min(int(batch_size), 1000))
        self.last_failures = 0

    async def preview_cleanup(self, *, now: datetime | None = None) -> dict[str, int]:
        """Count database cleanup candidates without mutating DB or blob state."""
        effective_now = _naive_utc(now)
        async with self._session_factory() as session:
            expired = list(
                (
                    await session.execute(
                        select(ContextPayload).where(
                            ContextPayload.expires_at.is_not(None),
                            ContextPayload.expires_at < effective_now,
                            ContextPayload.deleted_at.is_(None),
                        ).limit(self._batch_size)
                    )
                ).scalars().all()
            )
            orphan = list(
                (
                    await session.execute(
                        select(ContextPayload).where(
                            ContextPayload.status == "orphan_pending_gc",
                            ContextPayload.deleted_at.is_(None),
                        ).limit(self._batch_size)
                    )
                ).scalars().all()
            )
        return {
            "expired_candidates": len(expired),
            "orphan_candidates": len(orphan),
        }

    async def cleanup_expired(self, *, now: datetime | None = None) -> int:
        effective_now = _naive_utc(now)
        return await self._delete_rows(
            ContextPayload.expires_at.is_not(None),
            ContextPayload.expires_at < effective_now,
            now=effective_now,
        )

    async def cleanup_orphans(self) -> tuple[int, int]:
        effective_now = _naive_utc(None)
        backends = self._resolved_backends()
        db_cleaned = await self._delete_rows(
            ContextPayload.status == "orphan_pending_gc",
            now=effective_now,
        )

        active_keys: dict[str, set[tuple[int, str]]] = {
            name: set() for name in backends
        }
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(ContextPayload).where(
                            ContextPayload.deleted_at.is_(None),
                            ContextPayload.storage_backend.in_(tuple(backends)),
                        )
                    )
                ).scalars().all()
            )

            for row in rows:
                backend = backends.get(row.storage_backend)
                if backend is None:
                    continue
                try:
                    exists = backend.exists(row.user_id, row.storage_key)
                except Exception:  # noqa: BLE001 — reported in lifecycle stats
                    self.last_failures += 1
                    continue
                if exists:
                    active_keys[row.storage_backend].add((row.user_id, row.storage_key))
                    continue
                row.status = "missing_file"
                row.deleted_at = effective_now
                db_cleaned += 1
            await session.flush()
            await session.commit()

        blob_cleaned = 0
        for backend_name, backend in backends.items():
            try:
                candidates = backend.iterate_orphans()
            except Exception:  # noqa: BLE001 — one backend must not hide another
                self.last_failures += 1
                continue
            for owner, storage_key in candidates[: self._batch_size]:
                if (owner, storage_key) in active_keys[backend_name]:
                    continue
                try:
                    if backend.delete(owner, storage_key):
                        blob_cleaned += 1
                except Exception:  # noqa: BLE001
                    self.last_failures += 1
        return db_cleaned, blob_cleaned

    async def _delete_rows(self, *predicates, now: datetime) -> int:
        removed = 0
        backends = self._resolved_backends()
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(ContextPayload).where(
                            *predicates,
                            ContextPayload.deleted_at.is_(None),
                        ).limit(self._batch_size)
                    )
                ).scalars().all()
            )
            for row in rows:
                backend_name = (row.storage_backend or "").lower()
                backend = backends.get(backend_name)
                if backend_name != "inline" and backend is None:
                    self.last_failures += 1
                    continue
                if backend is not None:
                    try:
                        backend.delete(row.user_id, row.storage_key)
                    except Exception:  # noqa: BLE001 — leave DB row retryable
                        self.last_failures += 1
                        continue
                row.status = "deleted"
                row.deleted_at = now
                removed += 1
            await session.flush()
            await session.commit()
        return removed

    def _resolved_backends(self) -> dict[str, Any]:
        if self._backends is None:
            self._backends = build_external_payload_backends()
        return self._backends


def _naive_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is not None:
        return current.astimezone(timezone.utc).replace(tzinfo=None)
    return current


__all__ = ["ProductionPayloadGcStorage"]
