"""CE-05 WP-8 Retention/Cleanup Worker 测试。

覆盖：
  - dry-run 默认 true（env 控制）
  - 未获取锁 → RetentionLockError
  - 清理顺序含引用保护语义
  - Maintenance 包导出
"""

from __future__ import annotations

import asyncio
import inspect
import os

import pytest

from app.context_engine.maintenance.retention_worker import (
    DRY_RUN_DEFAULT,
    LOCK_NAME,
    RetentionLockError,
    RetentionWorker,
)


def test_dry_run_default_true():
    """CONTEXT_RETENTION_DRY_RUN 未设 → dry-run 默认 true。"""
    os.environ.pop("CONTEXT_RETENTION_DRY_RUN", None)
    assert RetentionWorker._env_dry_run() is True
    assert DRY_RUN_DEFAULT is True


def test_dry_run_env_false():
    os.environ["CONTEXT_RETENTION_DRY_RUN"] = "false"
    try:
        assert RetentionWorker._env_dry_run() is False
    finally:
        os.environ.pop("CONTEXT_RETENTION_DRY_RUN", None)


def test_lock_name_fixed():
    assert LOCK_NAME == "testagent:context-retention"


def test_lock_not_acquired_raises():
    """未获取锁 → RetentionLockError（不静默继续）。"""

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt, params=None):
            # GET_LOCK 返回 0（未获取）
            class _R:
                def scalar_one(self):
                    return 0
            return _R()

    def _lock_factory():
        return _FakeSession()

    worker = RetentionWorker(session_factory=_lock_factory, lock_session_factory=_lock_factory)
    import asyncio
    with pytest.raises(RetentionLockError):
        asyncio.run(worker.run_once())


def test_worker_dry_run_property():
    worker = RetentionWorker(session_factory=lambda: None)
    assert worker.dry_run is True
    os.environ["CONTEXT_RETENTION_DRY_RUN"] = "false"
    try:
        w2 = RetentionWorker(session_factory=lambda: None)
        assert w2.dry_run is False
    finally:
        os.environ.pop("CONTEXT_RETENTION_DRY_RUN", None)


def test_batch_size_capped():
    w = RetentionWorker(session_factory=lambda: None, batch_size=5000)
    assert w._batch_size <= 1000


def test_cleanup_runs_while_lock_session_is_still_active():
    """MySQL advisory locks are connection-bound and must outlive cleanup."""

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one(self):
            return self._value

    class _LockSession:
        def __init__(self):
            self.active = False
            self.info = {}
            self.release_active = None

        async def __aenter__(self):
            self.active = True
            return self

        async def __aexit__(self, *exc):
            self.active = False
            return False

        async def execute(self, stmt, params=None):
            sql = str(stmt)
            if "GET_LOCK" in sql:
                return _Result(1)
            if "CONNECTION_ID" in sql:
                return _Result(42)
            if "IS_USED_LOCK" in sql:
                return _Result(42)
            if "RELEASE_LOCK" in sql:
                self.release_active = self.active
                return _Result(1)
            raise AssertionError(sql)

    session = _LockSession()
    worker = RetentionWorker(
        session_factory=lambda: session,
        lock_session_factory=lambda: session,
        dry_run=True,
    )
    observed = {}

    async def _cleanup(*, lock_session):
        observed["active"] = lock_session.active
        return {
            "expired_payloads": 0,
            "orphan_payloads": 0,
            "superseded_summaries": 0,
            "event_rows": 0,
            "index_lease_recovered": 0,
        }

    worker._run_cleanup_cycle = _cleanup

    asyncio.run(worker.run_once())

    assert observed["active"] is True
    assert session.release_active is True


def test_retention_worker_lifecycle_runs_and_stops_cleanly():
    """The periodic worker must be lifecycle-manageable without leaking a task."""

    async def _exercise():
        worker = RetentionWorker(
            session_factory=lambda: None,
            interval_seconds=0.01,
        )
        first_run = asyncio.Event()

        async def _run_once():
            first_run.set()
            return {}

        worker.run_once = _run_once
        await worker.start()
        await asyncio.wait_for(first_run.wait(), timeout=0.5)
        await worker.stop()
        assert worker._task is None

    asyncio.run(_exercise())


def test_lifespan_wires_retention_worker_start_and_stop():
    from app import main as main_module

    source = inspect.getsource(main_module.lifespan)
    assert "RetentionWorker" in source
    assert "app.state.retention_worker" in source
    assert "await retention_worker.start()" in source
    yield_position = source.find("yield")
    assert source.find("await retention_worker.stop()", yield_position) > yield_position
