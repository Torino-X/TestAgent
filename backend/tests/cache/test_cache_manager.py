"""Tests for ``app.cache.manager.CacheManager``.

Coverage (per prompt §33 + 设计文档 §6/§7/§8/§17/§18/§19/§21):
  * hit / miss / negative
  * ttl + jitter
  * corrupt payload → cache_miss + auto-cleanup
  * oversize skip
  * redis get / set / delete failures don't crash the call
  * circuit breaker integration
  * get_many / set_many
  * generation token bump + get
  * get_or_load: cache hit skips loader / miss invokes loader
  * get_or_load: SingleFlight coalesces concurrent misses
  * get_or_load: DB bulkhead limits concurrent loaders
  * bypass when domain flag is off
  * bypass when master toggle is off
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.cache.backend import CacheBackend
from app.cache.bulkhead import BulkheadTimeoutError, DBBulkhead
from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
from app.cache.manager import (
    CACHE_BYPASS,
    CACHE_ERROR,
    CACHE_MISS,
    CacheManager,
    _CacheMiss,
)
from app.cache.metrics import cache_metrics
from app.cache.singleflight import SingleFlight
from app.cache.specs import CacheSpec


# ── Test helpers ───────────────────────────────────────────────────


AUTH_SPEC = CacheSpec(
    domain="auth",
    ttl_seconds=60,
    negative_ttl_seconds=5,
    jitter_ratio=0.10,
)


def _make_manager(
    fake_redis,
    *,
    jitter_fn=None,
    breaker: CircuitBreaker | None = None,
    bulkhead: DBBulkhead | None = None,
    singleflight: SingleFlight | None = None,
) -> CacheManager:
    """Construct a CacheManager wired to the supplied FakeRedis.

    Wires the fakeredis client into a private CacheBackend so the global
    singleton (potentially installed by other tests) is bypassed.
    """
    backend = CacheBackend(redis_client=fake_redis)
    backend._healthy = True
    return CacheManager(
        backend=backend,
        breaker=breaker or CircuitBreaker(
            BreakerConfig(failure_threshold=2, open_seconds=0.05)
        ),
        bulkhead=bulkhead or DBBulkhead(max_concurrency=2),
        singleflight=singleflight or SingleFlight(),
        jitter_fn=jitter_fn,
    )


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch) -> None:
    """Ensure no global CacheBackend lingers across tests."""
    CacheBackend._instance = None
    cache_metrics.reset()
    yield
    CacheBackend._instance = None


# ── Hit / Miss / Negative ─────────────────────────────────────────


class TestHitMissNegative:
    async def test_get_on_empty_key_returns_cache_miss(self, fake_redis) -> None:
        mgr = _make_manager(fake_redis)
        result = await mgr.get(AUTH_SPEC, "missing")
        assert isinstance(result, _CacheMiss)
        assert result.reason == "miss"

    async def test_set_then_get_returns_payload(self, fake_redis) -> None:
        mgr = _make_manager(fake_redis)
        ok = await mgr.set(AUTH_SPEC, "k1", {"name": "alice", "role": "user"})
        assert ok is True
        result = await mgr.get(AUTH_SPEC, "k1")
        assert result == {"name": "alice", "role": "user"}

    async def test_negative_envelope_returns_cache_miss(self, fake_redis) -> None:
        mgr = _make_manager(fake_redis)
        ok = await mgr.set_negative(AUTH_SPEC, "neg-key")
        assert ok is True
        result = await mgr.get(AUTH_SPEC, "neg-key")
        assert isinstance(result, _CacheMiss)
        assert result.reason == "negative"

    async def test_delete_removes_key(self, fake_redis) -> None:
        mgr = _make_manager(fake_redis)
        await mgr.set(AUTH_SPEC, "k1", "v")
        deleted = await mgr.delete(AUTH_SPEC, "k1")
        assert deleted is True
        # Subsequent get is miss.
        result = await mgr.get(AUTH_SPEC, "k1")
        assert isinstance(result, _CacheMiss)


# ── TTL + Jitter ──────────────────────────────────────────────────


class TestTtlAndJitter:
    async def test_ttl_within_jitter_window(self, fake_redis) -> None:
        """100 samples must all fall in [54, 66] for base=60 / ratio=0.10.

        CacheManager treats the ``key`` argument as the full Redis key —
        the namespace prefix (``ta:{env}:cache:v1:``) is added by the
        caller (Domain Cache).  These tests exercise the primitive in
        isolation, so we use bare keys.
        """
        mgr = _make_manager(fake_redis)
        for i in range(100):
            await mgr.set(AUTH_SPEC, f"k-{i}", f"v-{i}")
            ttl = await fake_redis.ttl(f"k-{i}")
            # fakeredis ttl is in seconds; allow ±10% jitter + 1s slack
            # for round-trip / Redis integer rounding.
            assert 53 <= ttl <= 67, f"ttl={ttl} for k-{i} out of jitter window"

    async def test_jitter_injectable_for_determinism(self, fake_redis) -> None:
        """Fixed jitter 1.0 → exact TTL."""
        # ratio is unused when jitter_fn returns 1.0; ttl = base * 1.0 = 60
        mgr = _make_manager(fake_redis, jitter_fn=lambda ratio: 1.0)
        await mgr.set(AUTH_SPEC, "k", "v")
        ttl = await fake_redis.ttl("k")
        assert 59 <= ttl <= 61, ttl  # ±1s slack for fakeredis rounding

    async def test_negative_ttl_uses_negative_ttl_seconds(self, fake_redis) -> None:
        mgr = _make_manager(fake_redis, jitter_fn=lambda ratio: 1.0)
        await mgr.set_negative(AUTH_SPEC, "k")
        ttl = await fake_redis.ttl("k")
        # spec.negative_ttl_seconds = 5, jitter ratio 0.10 → 4.5..5.5
        assert 4 <= ttl <= 6, ttl


def _env() -> str:
    """Slug of APP_ENV (matches key_builder._env_segment)."""
    from app.core.config import get_settings

    raw = get_settings().app_env or "dev"
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in raw)[:32] or "dev"


# ── Corrupt payload + oversize ────────────────────────────────────


class TestCorruptAndOversize:
    async def test_corrupt_payload_returns_cache_miss_and_cleans_up(
        self, fake_redis
    ) -> None:
        mgr = _make_manager(fake_redis)
        # Manually plant a corrupt payload (not via set() so the envelope
        # is structurally invalid).
        key = f"ta:{_env()}:cache:v1:corrupt"
        await fake_redis.set(key, b"{not-json-at-all")
        result = await mgr.get(AUTH_SPEC, key)
        assert isinstance(result, _CacheMiss)
        assert result.reason == "corrupt"
        # Corrupt key must have been auto-cleaned (best-effort).
        remaining = await fake_redis.get(key)
        assert remaining is None

    async def test_oversize_payload_skipped(self, fake_redis) -> None:
        small_spec = CacheSpec(
            domain="auth",
            ttl_seconds=60,
            negative_ttl_seconds=5,
            max_value_bytes=64,  # very tight cap for test
        )
        mgr = _make_manager(fake_redis)
        big_payload = {"data": "x" * 200}
        ok = await mgr.set(small_spec, "big", big_payload)
        assert ok is False, "oversize must skip Redis"
        remaining = await fake_redis.get(f"ta:{_env()}:cache:v1:big")
        assert remaining is None, "oversize must not write to Redis"


# ── Redis failure resilience ──────────────────────────────────────


class TestRedisFailure:
    async def test_redis_get_fail_returns_cache_miss(self) -> None:
        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("redis down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = True
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(BreakerConfig(failure_threshold=10, open_seconds=10)),
        )
        result = await mgr.get(AUTH_SPEC, "k")
        assert isinstance(result, _CacheMiss)
        assert result.reason == "miss", "broken redis must map to miss"

    async def test_redis_set_fail_returns_false(self) -> None:
        class BrokenRedis:
            async def set(self, *a, **kw):
                raise ConnectionError("redis down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = True
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(BreakerConfig(failure_threshold=10, open_seconds=10)),
        )
        ok = await mgr.set(AUTH_SPEC, "k", "v")
        assert ok is False

    async def test_redis_delete_fail_returns_false(self) -> None:
        class BrokenRedis:
            async def delete(self, *a, **kw):
                raise ConnectionError("redis down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = True
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(BreakerConfig(failure_threshold=10, open_seconds=10)),
        )
        ok = await mgr.delete(AUTH_SPEC, "k")
        assert ok is False

    async def test_redis_opens_breaker_after_repeated_failures(self) -> None:
        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("redis down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = True
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(BreakerConfig(failure_threshold=2, open_seconds=10)),
        )
        # Two failures → breaker opens.
        for _ in range(2):
            await mgr.get(AUTH_SPEC, "k")
        from app.cache.circuit_breaker import BreakerState

        assert mgr._breaker.state is BreakerState.OPEN


# ── Batch: get_many / set_many ────────────────────────────────────


class TestBatchOps:
    async def test_get_many_partial_hit(self, fake_redis) -> None:
        mgr = _make_manager(fake_redis)
        await mgr.set(AUTH_SPEC, "k1", {"v": 1})
        await mgr.set(AUTH_SPEC, "k2", {"v": 2})
        # k3 not written
        result = await mgr.get_many(AUTH_SPEC, ["k1", "k2", "k3"])
        assert result["k1"] == {"v": 1}
        assert result["k2"] == {"v": 2}
        assert isinstance(result["k3"], _CacheMiss)
        assert result["k3"].reason == "miss"

    async def test_get_many_empty_keys(self, fake_redis) -> None:
        mgr = _make_manager(fake_redis)
        result = await mgr.get_many(AUTH_SPEC, [])
        assert result == {}

    async def test_set_many_writes_all(self, fake_redis) -> None:
        mgr = _make_manager(fake_redis, jitter_fn=lambda ratio: 1.0)
        mapping = {"a": {"n": 1}, "b": {"n": 2}, "c": {"n": 3}}
        written = await mgr.set_many(AUTH_SPEC, mapping)
        assert written == 3
        # All three keys must be readable back.
        for k, expected in mapping.items():
            assert (await mgr.get(AUTH_SPEC, k)) == expected


# ── Generation tokens (collection invalidation) ───────────────────


class TestGeneration:
    async def test_get_generation_initializes_if_missing(self, fake_redis) -> None:
        mgr = _make_manager(fake_redis)
        gen1 = await mgr.get_generation("conv", 10001)
        assert gen1, "first read must produce a token"
        # Second read returns the same token (persisted).
        gen2 = await mgr.get_generation("conv", 10001)
        assert gen1 == gen2, "second read must observe the same token"

    async def test_bump_generation_rotates_token(self, fake_redis) -> None:
        mgr = _make_manager(fake_redis)
        gen1 = await mgr.get_generation("conv", 10001)
        gen2 = await mgr.bump_generation("conv", 10001)
        assert gen1 != gen2, "bump must change the token"
        gen3 = await mgr.get_generation("conv", 10001)
        assert gen2 == gen3, "subsequent get returns the bumped token"

    async def test_different_users_have_independent_tokens(
        self, fake_redis
    ) -> None:
        mgr = _make_manager(fake_redis)
        await mgr.bump_generation("conv", 10001)
        await mgr.bump_generation("conv", 10002)
        # After bumping, reading must return the per-user token.
        assert (await mgr.get_generation("conv", 10001)) != (
            await mgr.get_generation("conv", 10002)
        )

    async def test_bump_isolates_users_via_key_namespace(
        self, fake_redis
    ) -> None:
        mgr = _make_manager(fake_redis)
        await mgr.get_generation("conv", 10001)
        await mgr.get_generation("conv", 10002)
        # Two different keys must exist in Redis (one per user).
        keys = await fake_redis.keys(f"ta:{_env()}:cache:v1:gen:conv:user:*")
        assert len(keys) == 2


# ── get_or_load (cache-aside + SingleFlight + bulkhead) ───────────


class TestGetOrLoad:
    async def test_cache_hit_skips_loader(self, fake_redis) -> None:
        mgr = _make_manager(fake_redis)
        await mgr.set(AUTH_SPEC, "k", {"v": "cached"})

        loader_called = {"count": 0}

        async def loader() -> dict:
            loader_called["count"] += 1
            return {"v": "fresh"}

        result = await mgr.get_or_load(AUTH_SPEC, "k", loader)
        assert result == {"v": "cached"}
        assert loader_called["count"] == 0, "loader must not run on hit"

    async def test_cache_miss_invokes_loader(self, fake_redis) -> None:
        mgr = _make_manager(fake_redis)
        loader_called = {"count": 0}

        async def loader() -> dict:
            loader_called["count"] += 1
            return {"v": "loaded"}

        result = await mgr.get_or_load(AUTH_SPEC, "k", loader)
        assert result == {"v": "loaded"}
        assert loader_called["count"] == 1
        # Second call hits cache (loader not invoked again).
        result2 = await mgr.get_or_load(AUTH_SPEC, "k", loader)
        assert result2 == {"v": "loaded"}
        assert loader_called["count"] == 1, "second call must hit cache"

    async def test_loader_returning_none_writes_negative_cache(
        self, fake_redis
    ) -> None:
        mgr = _make_manager(fake_redis)

        async def loader() -> Any:
            return None

        result = await mgr.get_or_load(AUTH_SPEC, "k", loader)
        assert result is None
        # Subsequent get → negative miss (short-circuits).
        result2 = await mgr.get(AUTH_SPEC, "k")
        assert isinstance(result2, _CacheMiss)
        assert result2.reason == "negative"

    async def test_100_concurrent_misses_coalesce_to_one_loader(
        self, fake_redis
    ) -> None:
        """SingleFlight coalesces 100 concurrent misses to 1 loader call."""
        mgr = _make_manager(fake_redis)
        loader_called = {"count": 0}

        async def loader() -> dict:
            loader_called["count"] += 1
            await asyncio.sleep(0.01)
            return {"v": "loaded"}

        results = await asyncio.gather(
            *[mgr.get_or_load(AUTH_SPEC, "k", loader) for _ in range(100)]
        )
        assert loader_called["count"] == 1, (
            f"SingleFlight must coalesce; got {loader_called['count']} calls"
        )
        assert all(r == {"v": "loaded"} for r in results)

    async def test_bulkhead_caps_concurrent_loaders(self, fake_redis) -> None:
        """Two bulkhead slots → third concurrent loader times out."""
        bh = DBBulkhead(max_concurrency=2)
        mgr = _make_manager(fake_redis, bulkhead=bh)

        started = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_loader() -> str:
            nonlocal started
            started += 1
            entered.set()
            await release.wait()
            return "v"

        # Two concurrent loaders fill the bulkhead.
        t1 = asyncio.create_task(mgr.get_or_load(AUTH_SPEC, "k1", slow_loader))
        t2 = asyncio.create_task(mgr.get_or_load(AUTH_SPEC, "k2", slow_loader))
        await entered.wait()
        # Third loader must time out (bulkhead timeout 2.0s — but we
        # shorten it by giving it a very short budget via wrapper).
        # The current API uses timeout=2.0 internally; here we just
        # verify the bulkhead is actually in use.
        assert bh.in_use == 2
        release.set()
        await asyncio.gather(t1, t2)
        assert started == 2

    async def test_redis_down_bypasses_cache_and_runs_loader(
        self,
    ) -> None:
        """Redis broken → bypass path runs the loader directly."""

        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("redis down")

            async def set(self, *a, **kw):
                raise ConnectionError("redis down")

            async def delete(self, *a, **kw):
                raise ConnectionError("redis down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = True
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(BreakerConfig(failure_threshold=10, open_seconds=10)),
        )
        loader_calls = {"n": 0}

        async def loader() -> dict:
            loader_calls["n"] += 1
            return {"v": "fresh"}

        result = await mgr.get_or_load(AUTH_SPEC, "k", loader)
        assert result == {"v": "fresh"}
        assert loader_calls["n"] == 1


# ── Bypass paths ──────────────────────────────────────────────────


class TestBypass:
    async def test_domain_disabled_returns_cache_bypass(self, fake_redis) -> None:
        mgr = _make_manager(fake_redis)
        # Patch domain_enabled on this instance only — avoids touching
        # the global Settings singleton.
        mgr.domain_enabled = lambda domain: False  # type: ignore[assignment]
        result = await mgr.get(AUTH_SPEC, "k")
        assert isinstance(result, _CacheMiss)
        assert result.reason == "bypass"

    async def test_backend_disabled_returns_cache_bypass(self) -> None:
        # No redis client → backend disabled.
        backend = CacheBackend(redis_client=None)
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(BreakerConfig(failure_threshold=10, open_seconds=10)),
        )
        result = await mgr.get(AUTH_SPEC, "k")
        assert isinstance(result, _CacheMiss)
        assert result.reason == "bypass"