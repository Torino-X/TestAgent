"""Phase 2.8B — ``RedisInFlightRegistry`` graceful degrade 测试。

守禁令 #31:Redis 不可用不 raise 5xx;
* ``try_acquire`` 在 Redis=None / Redis 抛异常时返回 None(调用方 fallback in-memory)
* ``release`` 在 Redis=None / owner=None 时返回 False
* 关键:failures 是静默的,WARN 日志记录但不抛错
"""

from __future__ import annotations

import asyncio

import pytest

try:
    from fakeredis import aioredis as fakeredis_aioredis
    HAS_FAKEREDIS = True
except ImportError:  # pragma: no cover
    HAS_FAKEREDIS = False

from app.agent_runtime.persistence.redis_inflight_registry import (
    InflightOwner,
    RedisInFlightRegistry,
)


pytestmark = pytest.mark.skipif(
    not HAS_FAKEREDIS,
    reason="fakeredis not installed",
)


async def test_try_acquire_returns_none_when_redis_is_none() -> None:
    """``redis_client=None`` 时 ``try_acquire`` 返回 None(降级 in-memory)。"""
    reg = RedisInFlightRegistry(redis_client=None, worker_id="worker-A")
    owner = await reg.try_acquire("task-degrade", "langgraph")
    assert owner is None


async def test_release_returns_false_when_redis_is_none() -> None:
    """``redis_client=None`` 时 ``release`` 返回 False,no raise。"""
    reg = RedisInFlightRegistry(redis_client=None, worker_id="worker-A")
    fake_owner = InflightOwner(worker_id="worker-A", engine="langgraph", acquired_at=0.0)
    released = await reg.release("task-degrade", fake_owner)
    assert released is False


async def test_release_returns_false_when_owner_is_none() -> None:
    """``owner=None`` 时 ``release`` 返回 False(no-op)。"""
    reg = RedisInFlightRegistry(redis_client=None, worker_id="worker-A")
    released = await reg.release("task-x", None)
    assert released is False


async def test_try_acquire_returns_none_when_redis_set_raises() -> None:
    """Redis SET 抛异常(模拟真实 Redis 断连)→ try_acquire 返回 None,不抛。"""
    # 构造一个总是抛异常的 fake client
    class _BrokenRedis:
        async def set(self, *args, **kwargs):
            raise ConnectionError("simulated Redis down")

        async def get(self, *args, **kwargs):
            raise ConnectionError("simulated Redis down")

        async def delete(self, *args, **kwargs):
            raise ConnectionError("simulated Redis down")

        async def aclose(self):
            pass

    reg = RedisInFlightRegistry(redis_client=_BrokenRedis(), worker_id="worker-A")
    owner = await reg.try_acquire("task-broken", "langgraph")
    assert owner is None  # graceful degrade,no raise


async def test_release_returns_false_when_redis_get_raises() -> None:
    """Redis GET 抛异常 → release 返回 False,不抛。"""
    class _BrokenRedis:
        async def get(self, *args, **kwargs):
            raise ConnectionError("simulated Redis down")

        async def set(self, *args, **kwargs):
            raise ConnectionError("simulated Redis down")

        async def delete(self, *args, **kwargs):
            raise ConnectionError("simulated Redis down")

        async def aclose(self):
            pass

    reg = RedisInFlightRegistry(redis_client=_BrokenRedis(), worker_id="worker-A")
    owner = InflightOwner(worker_id="worker-A", engine="langgraph", acquired_at=0.0)
    released = await reg.release("task-broken", owner)
    assert released is False


async def test_release_returns_false_when_key_ttl_expired() -> None:
    """锁已被 TTL 过期(key 消失)→ release 返回 False。"""
    if not HAS_FAKEREDIS:
        pytest.skip("fakeredis not installed")

    redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
    try:
        reg = RedisInFlightRegistry(
            redis_client=redis, worker_id="worker-A", ttl_seconds=1
        )
        owner = await reg.try_acquire("task-ttl", "langgraph")
        assert owner is not None

        # 强制 DEL key(模拟 TTL 过期)
        await redis.flushall()

        released = await reg.release("task-ttl", owner)
        assert released is False
    finally:
        await redis.flushall()
        await redis.aclose()


async def test_aclose_handles_missing_aclose_method() -> None:
    """redis_client 没有 ``aclose`` 方法 → aclose 不抛。"""
    class _NoAcloseRedis:
        async def set(self, *args, **kwargs):
            return True

    reg = RedisInFlightRegistry(redis_client=_NoAcloseRedis(), worker_id="worker-A")
    await reg.aclose()  # 不抛
