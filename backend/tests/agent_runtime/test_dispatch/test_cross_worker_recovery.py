"""Phase 2.8B — 跨 worker 并行调度守护 + checkpoint 恢复 E2E。

策略:在同进程内启 2 个独立 ``RedisInFlightRegistry`` 实例(模拟 worker_A /
worker_B),共享 fakeredis 实例 + asyncio event loop。Windows/Linux 一致。

核心场景(对应 docs/30 §3.5 + ADR-2.8B-3):
1. Worker A acquire lock → Worker B 阻塞(抛 ParallelDispatchGuardError)
2. Worker A release → Worker B 拿到锁
3. 跨 worker 锁 TTL 兜底:flushall 后 acquire 仍能成功
4. 双 worker 并发 acquire 同 task → 只有一个拿到,另一个抛错
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

try:
    from fakeredis import aioredis as fakeredis_aioredis
    HAS_FAKEREDIS = True
except ImportError:  # pragma: no cover
    HAS_FAKEREDIS = False

from app.agent_runtime.dispatch_errors import ParallelDispatchGuardError
from app.agent_runtime.persistence.redis_inflight_registry import (
    InflightOwner,
    RedisInFlightRegistry,
)


pytestmark = pytest.mark.skipif(
    not HAS_FAKEREDIS,
    reason="fakeredis not installed; skip cross-worker E2E",
)


@pytest.fixture
async def shared_fake_redis():
    redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
    yield redis
    await redis.flushall()
    await redis.aclose()


async def test_worker_a_acquire_worker_b_blocked(shared_fake_redis) -> None:
    """Worker A acquire OK → Worker B 同 task acquire 抛 ParallelDispatchGuardError。"""
    reg_a = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-A")
    reg_b = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-B")

    owner_a = await reg_a.try_acquire("task-cross-1", "langgraph")
    assert owner_a is not None
    assert owner_a.worker_id == "worker-A"

    with pytest.raises(ParallelDispatchGuardError) as exc_info:
        await reg_b.try_acquire("task-cross-1", "langgraph")
    assert exc_info.value.task_public_id == "task-cross-1"
    assert exc_info.value.running_engine == "langgraph"

    # cleanup
    await reg_a.release("task-cross-1", owner_a)


async def test_worker_a_release_worker_b_can_acquire(shared_fake_redis) -> None:
    """Worker A release → Worker B 拿到锁,owner.worker_id = "worker-B"。"""
    reg_a = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-A")
    reg_b = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-B")

    owner_a = await reg_a.try_acquire("task-rel", "langgraph")
    assert owner_a is not None
    released = await reg_a.release("task-rel", owner_a)
    assert released is True

    owner_b = await reg_b.try_acquire("task-rel", "langgraph")
    assert owner_b is not None
    assert owner_b.worker_id == "worker-B"

    await reg_b.release("task-rel", owner_b)


async def test_concurrent_acquire_only_one_winner(shared_fake_redis) -> None:
    """并发场景:同 task 两次 acquire 用 asyncio.gather → 只一个成功,另一个抛错。

    验证 Redis SET NX 在 asyncio 并发场景下的原子性 — 这是跨 worker 守护的核心。
    """
    reg_a = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-A")
    reg_b = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-B")

    async def acquire(reg):
        try:
            return ("ok", await reg.try_acquire("task-race", "langgraph"))
        except ParallelDispatchGuardError as e:
            return ("guard", e)

    result_a, result_b = await asyncio.gather(acquire(reg_a), acquire(reg_b))
    statuses = sorted([result_a[0], result_b[0]])
    # 一个 ok + 一个 guard(精确顺序由 asyncio 调度决定)
    assert statuses == ["guard", "ok"], f"expected one ok + one guard; got {statuses}"

    # 拿到锁的那个清理
    if result_a[0] == "ok":
        await reg_a.release("task-race", result_a[1])
    else:
        await reg_b.release("task-race", result_b[1])


async def test_ttl_reclaim_via_flushall(shared_fake_redis) -> None:
    """TTL 兜底:flushall(模拟 TTL 过期)→ 后到的 worker 仍能 acquire。"""
    reg_a = RedisInFlightRegistry(
        redis_client=shared_fake_redis, worker_id="worker-A", ttl_seconds=600
    )
    reg_b = RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id="worker-B")

    owner_a = await reg_a.try_acquire("task-ttl", "langgraph")
    assert owner_a is not None

    # 模拟 TTL 过期(key 消失)
    await shared_fake_redis.flushall()

    # 此时 owner_a 已无法 release(返回 False)
    released = await reg_a.release("task-ttl", owner_a)
    assert released is False

    # Worker B 仍能 acquire
    owner_b = await reg_b.try_acquire("task-ttl", "langgraph")
    assert owner_b is not None
    await reg_b.release("task-ttl", owner_b)


async def test_three_workers_round_robin(shared_fake_redis) -> None:
    """3 个 worker 轮流 acquire → 严格串行(防止双 worker 同时跑)。"""
    regs = [
        RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id=f"worker-{i}")
        for i in ("A", "B", "C")
    ]

    for i, reg in enumerate(regs):
        owner = await reg.try_acquire("task-rr", "langgraph")
        assert owner is not None
        assert owner.worker_id == f"worker-{('A', 'B', 'C')[i]}"
        released = await reg.release("task-rr", owner)
        assert released is True


async def test_different_tasks_no_cross_blocking(shared_fake_redis) -> None:
    """不同 task 之间互不干扰:3 个 worker 在 3 个不同 task 上同时 acquire 都成功。"""
    regs = [
        RedisInFlightRegistry(redis_client=shared_fake_redis, worker_id=f"worker-{i}")
        for i in ("A", "B", "C")
    ]
    tasks = [f"task-{uuid.uuid4().hex[:6]}" for _ in range(3)]
    owners = await asyncio.gather(*[r.try_acquire(t, "langgraph") for r, t in zip(regs, tasks)])
    assert all(o is not None for o in owners)
    # cleanup
    await asyncio.gather(*[r.release(t, o) for r, t, o in zip(regs, tasks, owners)])
