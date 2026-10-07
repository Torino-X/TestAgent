"""Tests for ``app.cache.distributed_lock.CacheFillLock``.

Coverage (per prompt §12):
  * acquire returns owner when key free
  * second acquire for same key returns None
  * release via token Lua — only owner can release
  * different keys are independent
  * token mismatch leaves lock intact
"""

from __future__ import annotations

import pytest
from fakeredis import aioredis as fakeredis_aioredis


class TestAcquireRelease:
    async def test_first_acquire_returns_owner(self, fake_redis) -> None:
        from app.cache.distributed_lock import CacheFillLock

        lock = CacheFillLock(fake_redis, ttl_ms=2000)
        owner = await lock.acquire("k1")
        assert owner is not None
        assert owner.token

    async def test_second_acquire_returns_none(self, fake_redis) -> None:
        from app.cache.distributed_lock import CacheFillLock

        lock = CacheFillLock(fake_redis, ttl_ms=2000)
        owner1 = await lock.acquire("k1")
        owner2 = await lock.acquire("k1")
        assert owner1 is not None
        assert owner2 is None, "second acquire on same key must fail"

    async def test_release_unblocks_new_acquire(self, fake_redis) -> None:
        from app.cache.distributed_lock import CacheFillLock

        lock = CacheFillLock(fake_redis, ttl_ms=2000)
        owner1 = await lock.acquire("k1")
        assert owner1 is not None
        released = await lock.release("k1", owner1.token)
        assert released is True
        # Now another acquire succeeds.
        owner2 = await lock.acquire("k1")
        assert owner2 is not None

    async def test_independent_keys(self, fake_redis) -> None:
        from app.cache.distributed_lock import CacheFillLock

        lock = CacheFillLock(fake_redis, ttl_ms=2000)
        a = await lock.acquire("k1")
        b = await lock.acquire("k2")
        c = await lock.acquire("k3")
        assert a is not None and b is not None and c is not None


class TestTokenOwnership:
    async def test_wrong_token_does_not_release(self, fake_redis) -> None:
        from app.cache.distributed_lock import CacheFillLock

        lock = CacheFillLock(fake_redis, ttl_ms=2000)
        owner = await lock.acquire("k1")
        assert owner is not None
        # Attempt release with a fabricated token.
        released = await lock.release("k1", "definitely-not-the-right-token")
        assert released is False
        # Original owner should still hold the key.
        another = await lock.acquire("k1")
        assert another is None

    async def test_concurrent_acquires_only_one_wins(self, fake_redis) -> None:
        import asyncio

        from app.cache.distributed_lock import CacheFillLock

        lock = CacheFillLock(fake_redis, ttl_ms=2000)

        async def try_acquire() -> object:
            return await lock.acquire("hot-key")

        results = await asyncio.gather(*[try_acquire() for _ in range(20)])
        winners = [r for r in results if r is not None]
        losers = [r for r in results if r is None]
        assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}"
        assert len(losers) == 19


class TestAsyncContextManager:
    async def test_context_manager_releases_on_exit(self, fake_redis) -> None:
        from app.cache.distributed_lock import CacheFillLock

        lock = CacheFillLock(fake_redis, ttl_ms=2000)
        async with await lock.acquire("k1") as owner:
            assert owner.token
            # Lock should be held
            again = await lock.acquire("k1")
            assert again is None
        # After exit, key is free
        again = await lock.acquire("k1")
        assert again is not None


class TestPrefix:
    async def test_custom_prefix_isolates_keys(self, fake_redis) -> None:
        from app.cache.distributed_lock import CacheFillLock

        lock_a = CacheFillLock(fake_redis, ttl_ms=2000, key_prefix="lockA:")
        lock_b = CacheFillLock(fake_redis, ttl_ms=2000, key_prefix="lockB:")
        # Same logical key, different prefixes → independent.
        a = await lock_a.acquire("k1")
        b = await lock_b.acquire("k1")
        assert a is not None and b is not None


class TestFailSoft:
    async def test_acquire_with_broken_redis_returns_none(self) -> None:
        from app.cache.distributed_lock import CacheFillLock

        class BrokenRedis:
            async def set(self, *args, **kwargs):
                raise ConnectionError("redis down")

        lock = CacheFillLock(BrokenRedis(), ttl_ms=2000)
        owner = await lock.acquire("k1")
        assert owner is None  # fail-soft: caller treats as "couldn't lock"

    async def test_release_with_broken_redis_returns_false(self) -> None:
        from app.cache.distributed_lock import CacheFillLock

        class BrokenRedis:
            async def eval(self, *args, **kwargs):
                raise ConnectionError("redis down")

        lock = CacheFillLock(BrokenRedis(), ttl_ms=2000)
        released = await lock.release("k1", "token-x")
        assert released is False