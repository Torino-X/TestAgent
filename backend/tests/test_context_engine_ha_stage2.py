"""Stage 2 contracts for Retention and Context Payload lifecycle safety."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


def _payload_row(**overrides):
    values = {
        "public_id": "pay_stage2",
        "user_id": 1,
        "source_type": "context_engine",
        "payload_type": "context_payload",
        "storage_backend": "inline",
        "storage_key": "stage2-key",
        "storage_key_hash": "h" * 64,
        "size_bytes": 10,
        "sha256": "s" * 64,
        "status": "active",
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "expires_at": None,
        "deleted_at": None,
    }
    values.update(overrides)
    return values


def test_payload_download_rejects_deleted_expired_and_inline_rows() -> None:
    from app.api.v1.context_payload import _validate_downloadable_payload

    now = datetime.now(timezone.utc)
    with pytest.raises(HTTPException) as deleted:
        _validate_downloadable_payload(
            SimpleNamespace(**_payload_row(status="deleted")), now=now
        )
    assert deleted.value.status_code == 410

    with pytest.raises(HTTPException) as expired:
        _validate_downloadable_payload(
            SimpleNamespace(
                **_payload_row(expires_at=(now - timedelta(seconds=1)).replace(tzinfo=None))
            ),
            now=now,
        )
    assert expired.value.status_code == 410

    with pytest.raises(HTTPException) as inline:
        _validate_downloadable_payload(SimpleNamespace(**_payload_row()), now=now)
    assert inline.value.status_code == 409


def test_filesystem_payload_backend_uses_explicit_root(tmp_path: Path) -> None:
    from app.context_engine.payload.factory import build_payload_backend

    backend = build_payload_backend("fs", fs_root=tmp_path)
    assert backend is not None
    backend.write(1, "key", b"payload")
    assert (tmp_path / "1" / "key").read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_payload_gc_preview_is_read_only(sqlite_session_factory) -> None:
    from app.context_engine.payload.gc_storage import ProductionPayloadGcStorage
    from app.models.context_engine import ContextPayload
    from app.repositories.base import ensure_model_id

    expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    async with sqlite_session_factory() as session:
        row = ContextPayload(**_payload_row(expires_at=expired_at))
        await ensure_model_id(session, ContextPayload, row)
        session.add(row)
        await session.commit()
        row_id = row.id

    storage = ProductionPayloadGcStorage(
        session_factory=sqlite_session_factory,
        backends={},
    )
    stats = await storage.preview_cleanup()

    assert stats["expired_candidates"] == 1
    async with sqlite_session_factory() as session:
        persisted = await session.get(ContextPayload, row_id)
        assert persisted.status == "active"
        assert persisted.deleted_at is None


@pytest.mark.asyncio
async def test_payload_gc_preview_does_not_construct_storage_backends(
    sqlite_session_factory, monkeypatch
) -> None:
    import app.context_engine.payload.gc_storage as gc_module

    def unexpected_backend_construction():
        raise AssertionError("dry-run must not create or connect to blob backends")

    monkeypatch.setattr(
        gc_module, "build_external_payload_backends", unexpected_backend_construction
    )
    storage = gc_module.ProductionPayloadGcStorage(
        session_factory=sqlite_session_factory
    )
    assert await storage.preview_cleanup() == {
        "expired_candidates": 0,
        "orphan_candidates": 0,
    }


@pytest.mark.asyncio
async def test_payload_gc_removes_blob_before_marking_row_deleted(
    sqlite_session_factory, tmp_path: Path
) -> None:
    from app.context_engine.payload import FileSystemPayloadBackend
    from app.context_engine.payload.gc_storage import ProductionPayloadGcStorage
    from app.models.context_engine import ContextPayload
    from app.repositories.base import ensure_model_id

    backend = FileSystemPayloadBackend(tmp_path)
    backend.write(1, "stage2-key", b"payload")
    expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    async with sqlite_session_factory() as session:
        row = ContextPayload(
            **_payload_row(storage_backend="fs", expires_at=expired_at)
        )
        await ensure_model_id(session, ContextPayload, row)
        session.add(row)
        await session.commit()
        row_id = row.id

    storage = ProductionPayloadGcStorage(
        session_factory=sqlite_session_factory,
        backends={"fs": backend},
    )
    assert await storage.cleanup_expired() == 1
    assert not backend.exists(1, "stage2-key")
    async with sqlite_session_factory() as session:
        persisted = await session.get(ContextPayload, row_id)
        assert persisted.status == "deleted"
        assert persisted.deleted_at is not None


@pytest.mark.asyncio
async def test_payload_open_rejects_expired_row_before_blob_read() -> None:
    from contextlib import asynccontextmanager

    from app.context_engine.errors import ContextEngineFailure
    from app.context_engine.payload.payload_storage import PayloadStorageService

    row = SimpleNamespace(
        **_payload_row(
            storage_backend="fs",
            expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(seconds=1),
        )
    )

    class Repo:
        async def get_by_public_id(self, public_id, user_id):
            return row

    class Backend:
        def read(self, *args):
            raise AssertionError("expired payload must not read blob storage")

    @asynccontextmanager
    async def session_factory():
        yield object()

    service = PayloadStorageService(Repo(), backend=Backend())
    with pytest.raises(ContextEngineFailure) as exc:
        async for _ in service.open(1, row.public_id, session_factory=session_factory):
            pass
    assert exc.value.error.code == "context.payload.gone"


@pytest.mark.asyncio
async def test_compaction_gc_dry_run_reports_candidates_without_mutation(
    sqlite_session_factory,
) -> None:
    from app.context_engine.compression.audit import CompactionGcService
    from app.context_engine.payload.gc_storage import ProductionPayloadGcStorage
    from app.models.context_engine import ContextPayload
    from app.repositories.base import ensure_model_id

    async with sqlite_session_factory() as session:
        row = ContextPayload(
            **_payload_row(
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(hours=1)
            )
        )
        await ensure_model_id(session, ContextPayload, row)
        session.add(row)
        await session.commit()

    gc = CompactionGcService(
        session_factory=sqlite_session_factory,
        payload_storage=ProductionPayloadGcStorage(
            session_factory=sqlite_session_factory,
            backends={},
        ),
    )
    stats = await gc.preview_gc()
    assert stats["expired_candidates"] == 1


def test_retention_worker_constructs_production_payload_gc() -> None:
    from inspect import getsource

    from app.context_engine.maintenance.retention_worker import RetentionWorker

    source = getsource(RetentionWorker._run_cleanup_cycle)
    assert "ProductionPayloadGcStorage" in source
    assert "preview_gc" in source
    assert "payload_storage=None" not in source


@pytest.mark.asyncio
async def test_retention_dry_run_counts_candidates_without_writes(
    sqlite_session_factory,
) -> None:
    from app.context_engine.maintenance.retention_worker import RetentionWorker
    from app.models.context_engine import ContextPayload
    from app.repositories.base import ensure_model_id

    async with sqlite_session_factory() as session:
        row = ContextPayload(
            **_payload_row(
                expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(hours=1)
            )
        )
        await ensure_model_id(session, ContextPayload, row)
        session.add(row)
        await session.commit()
        row_id = row.id

    class _EventRetentionPolicy:
        def __init__(self, *, session_factory):
            self._session_factory = session_factory

        async def preview_once(self):
            return {"total_candidates": 0}

    worker = RetentionWorker(
        session_factory=sqlite_session_factory,
        dry_run=True,
        event_retention_policy_factory=_EventRetentionPolicy,
    )

    async def lock_is_owned(_session):
        return None

    worker._assert_lock_owned = lock_is_owned
    stats = await worker._run_cleanup_cycle(lock_session=object())

    assert stats["expired_payloads"] == 1
    assert stats["degraded"] is False
    async with sqlite_session_factory() as session:
        persisted = await session.get(ContextPayload, row_id)
        assert persisted.status == "active"
        assert persisted.deleted_at is None
