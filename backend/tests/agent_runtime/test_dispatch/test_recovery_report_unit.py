"""Phase 2.8B — ``cross_worker_recovery`` helpers 单元测试(无需真实 Postgres)。

验证 ``scan_recoverable_checkpoints`` / ``release_orphaned_lock`` 对 None / empty / error 边界条件。
"""

from __future__ import annotations

import pytest

from app.agent_runtime.persistence.cross_worker_recovery import (
    RecoveryReport,
    release_orphaned_lock,
    scan_recoverable_checkpoints,
)


async def test_scan_recoverable_checkpoints_raises_on_none_cp() -> None:
    """``cp=None`` 时 ``scan_recoverable_checkpoints`` 抛 RuntimeError。"""
    with pytest.raises(RuntimeError, match="cp is None"):
        await scan_recoverable_checkpoints(cp=None, redis_inflight=None)


async def test_scan_recoverable_checkpoints_no_list_api_fallback_empty() -> None:
    """``cp.list`` 不存在 → 空 result,scanned=0,redis_lock_skipped=True。"""

    class _FakeCPNoList:
        """no list method — fallback"""

    report = await scan_recoverable_checkpoints(
        cp=_FakeCPNoList(), redis_inflight=None
    )
    assert isinstance(report, RecoveryReport)
    assert report.scanned_thread_count == 0
    assert report.recoverable_thread_ids == []
    assert report.redis_lock_skipped is True


async def test_scan_recoverable_checkpoints_with_empty_list() -> None:
    """``cp.list`` 返回空 → empty report。"""

    class _EmptyAsyncIter:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _FakeCPEmpty:
        def list(self, config):
            return _EmptyAsyncIter()

    report = await scan_recoverable_checkpoints(
        cp=_FakeCPEmpty(), redis_inflight=None
    )
    assert report.scanned_thread_count == 0
    assert report.redis_lock_skipped is True


async def test_scan_recoverable_checkpoints_filters_by_metadata_source() -> None:
    """``cp.list`` 返回 2 条 — 仅 metadata.source == "interrupted" 的进 recoverable。"""

    class _CheckpointTuple:
        def __init__(self, thread_id, metadata_source):
            self.config = {"configurable": {"thread_id": thread_id}}
            self.checkpoint = {
                "v": 1,
                "metadata": {"source": metadata_source},
            }

    class _AsyncIter:
        def __init__(self, items):
            self._items = items
            self._idx = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._idx >= len(self._items):
                raise StopAsyncIteration
            item = self._items[self._idx]
            self._idx += 1
            return item

    class _FakeCPMixed:
        def list(self, config):
            return _AsyncIter([
                _CheckpointTuple("t-int", "interrupted"),
                _CheckpointTuple("t-other", "completed"),
            ])

    report = await scan_recoverable_checkpoints(
        cp=_FakeCPMixed(), redis_inflight=None
    )
    assert report.scanned_thread_count == 2
    assert "t-int" in report.recoverable_thread_ids
    assert "t-other" in report.metadata_mismatch_thread_ids


async def test_release_orphaned_lock_returns_false_when_redis_none() -> None:
    """``redis_inflight=None`` 时 ``release_orphaned_lock`` 返回 False。"""
    result = await release_orphaned_lock(
        redis_inflight=None, task_public_id="t-x"
    )
    assert result is False


async def test_release_orphaned_lock_returns_false_when_no_inner_redis() -> None:
    """``redis_inflight._redis=None`` 时返回 False。"""

    class _RegNoInner:
        _redis = None
        key_prefix = "inflight:"

    result = await release_orphaned_lock(
        redis_inflight=_RegNoInner(), task_public_id="t-x"
    )
    assert result is False


async def test_release_orphaned_lock_deletes_key() -> None:
    """正常路径:redis_inflight._redis.delete(key) → True。"""

    class _FakeRedis:
        def __init__(self):
            self.deleted = []

        async def delete(self, key):
            self.deleted.append(key)
            return 1

    class _Reg:
        def __init__(self, redis):
            self._redis = redis
            self.key_prefix = "inflight:"

    fake = _FakeRedis()
    result = await release_orphaned_lock(redis_inflight=_Reg(fake), task_public_id="t-del")
    assert result is True
    assert fake.deleted == ["inflight:t-del"]


async def test_release_orphaned_lock_handles_redis_error() -> None:
    """Redis DELETE 抛异常 → 返回 False,不抛。"""

    class _BrokenRedis:
        async def delete(self, key):
            raise ConnectionError("simulated outage")

    class _Reg:
        _redis = _BrokenRedis()
        key_prefix = "inflight:"

    result = await release_orphaned_lock(redis_inflight=_Reg(), task_public_id="t-x")
    assert result is False
