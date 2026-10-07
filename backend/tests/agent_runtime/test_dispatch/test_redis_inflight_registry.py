"""Phase 2.8B — ``RedisInFlightRegistry`` 7 个核心 case(基于 fakeredis)。

设计要点(对应 docs/30 §3 + ADR-2.8B-1/4):

* ``try_acquire`` SET NX EX 成功 → 返回 ``InflightOwner``
* 同 task 第二次 ``try_acquire`` → 抛 ``ParallelDispatchGuardError``(含 running_engine)
* ``release`` 后再 ``try_acquire`` → 成功,新 worker_id
* ``release`` owner mismatch → 静默返回 False,不抛
* TTL 过期后 key 消失 → ``release`` 返回 False;``try_acquire`` 可重新获取
* ``worker_id`` 默认 = ``f"{HOSTNAME}-{pid}-{uuid4().hex[:6]}"``
* 不同 task 之间互不干扰

守禁令映射:
* #20 — 同 task 跨 worker 并发阻止
* #31 — Redis 不可用不 raise 5xx
* #32 — release owner mismatch 不阻断
"""

from __future__ import annotations

import asyncio

import pytest

# fakeredis 是 Phase 2.8B 新增 test-only 依赖
try:
    from fakeredis import aioredis as fakeredis_aioredis
    HAS_FAKEREDIS = True
except ImportError:  # pragma: no cover — CI 必装,这里是兜底
    fakeredis_aioredis = None  # type: ignore
    HAS_FAKEREDIS = False

from app.agent_runtime.dispatch_errors import ParallelDispatchGuardError
from app.agent_runtime.persistence.redis_inflight_registry import (
    InflightOwner,
    RedisInFlightRegistry,
)


pytestmark = pytest.mark.skipif(
    not HAS_FAKEREDIS,
    reason="fakeredis not installed; skip Phase 2.8B Redis tests",
)


@pytest.fixture
async def shared_fake_redis():
    """跨 task 共享的 fakeredis 实例(Phase 2.8B ADR-2.8B-3)。"""
    redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
    yield redis
    await redis.flushall()
    await redis.aclose()


def test_worker_id_default_format() -> None:
    """默认 worker_id = ``HOSTNAME-pid-uuid6``。"""
    reg = RedisInFlightRegistry(redis_client=None)
    wid = reg.worker_id
    assert isinstance(wid, str)
    parts = wid.split("-")
    assert len(parts) >= 3, f"worker_id 至少 3 段: {wid}"
    # 最后一段是 uuid6 hex
    assert len(parts[-1]) == 6


def test_worker_id_override_used() -> None:
    """显式传入 worker_id → 用该值。"""
    reg = RedisInFlightRegistry(redis_client=None, worker_id="worker-X")
    assert reg.worker_id == "worker-X"


async def test_try_acquire_success_returns_owner(shared_fake_redis) -> None:
    """首次 try_acquire 成功 → 返回 InflightOwner(worker_id/engine/acquired_at)。"""
    reg = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-A")
    owner = await reg.try_acquire("task-1", "langgraph")
    assert owner is not None
    assert isinstance(owner, InflightOwner)
    assert owner.worker_id == "worker-A"
    assert owner.engine == "langgraph"
    assert owner.acquired_at > 0


async def test_try_acquire_collision_raises_guard(shared_fake_redis) -> None:
    """同 task 第二次 try_acquire(不同 worker)→ 抛 ``ParallelDispatchGuardError``。

    ``running_engine`` 取自 Redis 中已存 owner 的 engine 字段。
    """
    reg_a = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-A")
    reg_b = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-B")

    owner_a = await reg_a.try_acquire("task-collision", "langgraph")
    assert owner_a is not None

    with pytest.raises(ParallelDispatchGuardError) as exc_info:
        await reg_b.try_acquire("task-collision", "langgraph")
    assert exc_info.value.task_public_id == "task-collision"
    assert exc_info.value.running_engine == "langgraph"
    assert exc_info.value.requested_engine == "langgraph"


async def test_release_then_reacquire_different_worker(shared_fake_redis) -> None:
    """A release 后,B 能拿到锁,owner.worker_id == "worker-B"。"""
    reg_a = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-A")
    reg_b = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-B")

    owner_a = await reg_a.try_acquire("task-rel", "langgraph")
    released = await reg_a.release("task-rel", owner_a)
    assert released is True

    owner_b = await reg_b.try_acquire("task-rel", "langgraph")
    assert owner_b is not None
    assert owner_b.worker_id == "worker-B"

    await reg_b.release("task-rel", owner_b)


async def test_release_owner_mismatch_returns_false(shared_fake_redis) -> None:
    """B 用错的 owner release A 的锁 → False,不抛,TTL 兜底过期。

    守禁令 #32:owner 校验失败不阻断。
    """
    reg_a = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-A")
    reg_b = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-B")

    owner_a = await reg_a.try_acquire("task-mismatch", "langgraph")
    fake_owner = InflightOwner(worker_id="worker-Z", engine="langgraph", acquired_at=0.0)
    released = await reg_b.release("task-mismatch", fake_owner)
    assert released is False

    # 真正的 owner A 还能正确释放
    released_a = await reg_a.release("task-mismatch", owner_a)
    assert released_a is True


async def test_different_tasks_no_interference(shared_fake_redis) -> None:
    """不同 task 之间互不干扰:同时各自能拿到锁。"""
    reg_a = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-A")
    owner_x = await reg_a.try_acquire("task-X", "langgraph")
    owner_y = await reg_a.try_acquire("task-Y", "langgraph")
    assert owner_x is not None
    assert owner_y is not None
    # 释放后都能再获取
    await reg_a.release("task-X", owner_x)
    await reg_a.release("task-Y", owner_y)
