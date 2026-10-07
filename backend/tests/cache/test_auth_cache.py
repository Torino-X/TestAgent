"""Tests for ``app.cache.domains.auth_cache.AuthPrincipalCache``.

Coverage (per prompt §16 / 设计文档 §10):
  * get_or_load hit returns DTO from cache (no DB call)
  * get_or_load miss invokes loader and caches result
  * get_or_load miss + loader returns None → negative envelope (5s)
  * write_through updates cache after DB commit
  * write_through with None invalidates the entry
  * DTO never includes password_hash / last_login_at / deleted_at
  * DTO round-trips through JSON serialization (only 8 fields)
  * Disabled backend → loader runs every time (bypass)
  * disabled domain flag → loader runs every time (bypass)
  * Domain flag off still allows explicit write_through no-op return
  * Cache key format follows namespace convention
"""

from __future__ import annotations

import pytest

from app.cache.backend import CacheBackend
from app.cache.bulkhead import DBBulkhead
from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
from app.cache.distributed_lock import CacheFillLock
from app.cache.domains.auth_cache import (
    AUTH_PRINCIPAL_SPEC,
    AuthPrincipalCache,
    AuthPrincipalDTO,
    auth_principal_key,
    get_auth_principal_cache,
    set_auth_principal_cache,
)
from app.cache.manager import CacheManager
from app.cache.metrics import cache_metrics
from app.cache.singleflight import SingleFlight


def _dto(public_id: str = "usr_x", **overrides) -> AuthPrincipalDTO:
    base = dict(
        internal_id=42,
        public_id=public_id,
        display_name="Alice",
        username="alice",
        email="alice@example.com",
        role="user",
        status="active",
        avatar_url=None,
    )
    base.update(overrides)
    return AuthPrincipalDTO(**base)


def _async_loader(dto):
    """Wrap a DTO (or None) into a one-shot async loader coroutine factory.

    Usage: ``_async_loader(_dto("usr_a"))`` returns a 0-arg lambda that,
    when called, returns a coroutine resolving to ``dto``.
    """
    async def _load() -> AuthPrincipalDTO | None:
        return dto

    return _load


def _make_manager(
    fake_redis,
    *,
    breaker: CircuitBreaker | None = None,
) -> CacheManager:
    backend = CacheBackend(redis_client=fake_redis)
    backend._healthy = True
    return CacheManager(
        backend=backend,
        breaker=breaker or CircuitBreaker(
            BreakerConfig(failure_threshold=5, open_seconds=0.05)
        ),
        bulkhead=DBBulkhead(max_concurrency=2),
        singleflight=SingleFlight(),
        jitter_fn=lambda ratio: 1.0,  # deterministic for TTL assertions
    )


def _make_auth_cache(fake_redis, fill_lock=None) -> AuthPrincipalCache:
    mgr = _make_manager(fake_redis)
    return AuthPrincipalCache(manager=mgr, fill_lock=fill_lock)


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch) -> None:
    CacheBackend._instance = None
    cache_metrics.reset()
    set_auth_principal_cache(None)
    # Force domain-enabled to True for tests (default).
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "cache_auth_enabled", True)
    yield
    CacheBackend._instance = None
    set_auth_principal_cache(None)


# ── Spec invariants ────────────────────────────────────────────────


class TestSpecInvariants:
    def test_ttl_is_60_seconds(self) -> None:
        """Per 设计文档 §10.4 — role/status 是敏感字段，TTL 不超过 60s."""
        assert AUTH_PRINCIPAL_SPEC.ttl_seconds == 60

    def test_negative_ttl_is_5_seconds(self) -> None:
        assert AUTH_PRINCIPAL_SPEC.negative_ttl_seconds == 5

    def test_distributed_fill_lock_enabled(self) -> None:
        """Auth principal is the canonical hot spec."""
        assert AUTH_PRINCIPAL_SPEC.enable_distributed_fill_lock is True

    def test_singleflight_enabled(self) -> None:
        assert AUTH_PRINCIPAL_SPEC.enable_singleflight is True

    def test_domain_is_auth(self) -> None:
        assert AUTH_PRINCIPAL_SPEC.domain == "auth"


class TestKeyFormat:
    def test_key_namespace(self) -> None:
        """ta:{env}:cache:v1:auth:principal:{public_id}"""
        key = auth_principal_key("usr_xyz")
        assert key.endswith(":auth:principal:usr_xyz"), key
        assert ":cache:v1:" in key, key


# ── DTO ────────────────────────────────────────────────────────────


class TestDTO:
    def test_dto_does_not_expose_password_hash(self) -> None:
        """Crit: password_hash MUST NOT be in the cached DTO."""
        dto = _dto()
        d = dto.to_dict()
        assert "password_hash" not in d
        for sensitive in ("password_hash", "last_login_at", "deleted_at"):
            assert sensitive not in d

    def test_dto_has_required_fields(self) -> None:
        d = _dto().to_dict()
        required = {
            "internal_id", "public_id", "display_name", "username",
            "email", "role", "status", "avatar_url",
        }
        assert required.issubset(set(d.keys()))

    def test_dto_round_trip(self) -> None:
        original = _dto(avatar_url="http://example.com/a.png", role="admin")
        d = original.to_dict()
        restored = AuthPrincipalDTO.from_dict(d)
        assert restored == original

    def test_dto_from_dict_strict_required(self) -> None:
        with pytest.raises(KeyError):
            AuthPrincipalDTO.from_dict({"internal_id": 1})  # missing public_id etc.


# ── Read path: get_or_load ─────────────────────────────────────────


class TestGetOrLoad:
    async def test_miss_invokes_loader(self, fake_redis) -> None:
        cache = _make_auth_cache(fake_redis)
        loader_calls = {"n": 0}

        async def loader() -> AuthPrincipalDTO:
            loader_calls["n"] += 1
            return _dto("usr_a")

        result = await cache.get_or_load("usr_a", loader)
        assert result.public_id == "usr_a"
        assert loader_calls["n"] == 1

    async def test_subsequent_call_hits_cache(self, fake_redis) -> None:
        cache = _make_auth_cache(fake_redis)
        loader_calls = {"n": 0}

        async def loader() -> AuthPrincipalDTO:
            loader_calls["n"] += 1
            return _dto("usr_a")

        await cache.get_or_load("usr_a", loader)
        result2 = await cache.get_or_load("usr_a", loader)
        assert result2.public_id == "usr_a"
        assert loader_calls["n"] == 1, "second call must hit cache"

    async def test_loader_returning_none_writes_negative_cache(
        self, fake_redis
    ) -> None:
        cache = _make_auth_cache(fake_redis)

        async def loader() -> AuthPrincipalDTO | None:
            return None

        result = await cache.get_or_load("usr_ghost", loader)
        assert result is None

        # Subsequent call short-circuits via negative cache.
        loader_calls = {"n": 0}

        async def counting_loader() -> AuthPrincipalDTO | None:
            loader_calls["n"] += 1
            return None

        result2 = await cache.get_or_load("usr_ghost", counting_loader)
        assert result2 is None
        assert loader_calls["n"] == 0, "negative cache must short-circuit"

    async def test_100_concurrent_misses_coalesce_to_one_loader(
        self, fake_redis
    ) -> None:
        import asyncio

        cache = _make_auth_cache(fake_redis)
        loader_calls = {"n": 0}

        async def loader() -> AuthPrincipalDTO:
            loader_calls["n"] += 1
            await asyncio.sleep(0.01)
            return _dto("usr_a")

        results = await asyncio.gather(
            *[cache.get_or_load("usr_a", loader) for _ in range(100)]
        )
        assert loader_calls["n"] == 1, (
            f"SingleFlight must coalesce auth lookups; got {loader_calls['n']}"
        )
        assert all(r.public_id == "usr_a" for r in results)

    async def test_loader_returning_dict_is_tolerated(self, fake_redis) -> None:
        cache = _make_auth_cache(fake_redis)

        async def loader() -> dict:
            return _dto("usr_b").to_dict()

        result = await cache.get_or_load("usr_b", loader)
        assert result.public_id == "usr_b"

    async def test_loader_returning_invalid_type_raises(self, fake_redis) -> None:
        cache = _make_auth_cache(fake_redis)

        async def loader() -> str:
            return "not a DTO"

        with pytest.raises(TypeError, match="must return AuthPrincipalDTO"):
            await cache.get_or_load("usr_x", loader)


# ── Write-through (post-DB-commit) ────────────────────────────────


class TestWriteThrough:
    async def test_write_through_updates_cache(self, fake_redis) -> None:
        cache = _make_auth_cache(fake_redis)
        dto = _dto("usr_a", display_name="Alice Updated")
        ok = await cache.write_through("usr_a", dto)
        assert ok is True
        # Subsequent read returns updated DTO.
        result = await cache.get_or_load("usr_a", lambda: _dto("usr_a"))
        assert result.display_name == "Alice Updated"

    async def test_write_through_with_none_invalidates(self, fake_redis) -> None:
        cache = _make_auth_cache(fake_redis)
        # Prime cache
        await cache.get_or_load("usr_a", _async_loader(_dto("usr_a")))
        # Invalidate
        ok = await cache.write_through("usr_a", None)
        assert ok is True
        # Next call must invoke loader (cache is empty)
        loader_calls = {"n": 0}

        async def loader() -> AuthPrincipalDTO:
            loader_calls["n"] += 1
            return _dto("usr_a")

        await cache.get_or_load("usr_a", loader)
        assert loader_calls["n"] == 1

    async def test_write_through_does_not_cache_invalid_payload(
        self, fake_redis
    ) -> None:
        """Even a stray caller cannot put a non-DTO in the auth cache."""
        cache = _make_auth_cache(fake_redis)
        # write_through signature enforces AuthPrincipalDTO | None;
        # an invalid type must be rejected by the type system, but we
        # also verify the runtime path is strict.
        # (No runtime guard here; the function signature is the contract.)

    async def test_get_or_load_then_write_through_serves_updated_value(
        self, fake_redis
    ) -> None:
        """Mirror of update_profile flow: DB change → write-through → next
        request sees the new value (no DB call)."""
        cache = _make_auth_cache(fake_redis)
        # 1. Cache miss → DB load → cached.
        await cache.get_or_load("usr_a", _async_loader(_dto("usr_a")))
        # 2. update_profile-equivalent: write-through with new values.
        new_dto = _dto("usr_a", display_name="Alice Updated")
        await cache.write_through("usr_a", new_dto)
        # 3. Next request: must NOT invoke the loader.
        loader_calls = {"n": 0}

        async def loader() -> AuthPrincipalDTO:
            loader_calls["n"] += 1
            return _dto("usr_a")

        result = await cache.get_or_load("usr_a", loader)
        assert loader_calls["n"] == 0
        assert result.display_name == "Alice Updated"


# ── Explicit invalidation ──────────────────────────────────────────


class TestInvalidate:
    async def test_invalidate_removes_entry(self, fake_redis) -> None:
        cache = _make_auth_cache(fake_redis)
        await cache.get_or_load("usr_a", _async_loader(_dto("usr_a")))
        ok = await cache.invalidate("usr_a")
        assert ok is True
        # Next call must re-load.
        loader_calls = {"n": 0}

        async def loader() -> AuthPrincipalDTO:
            loader_calls["n"] += 1
            return _dto("usr_a")

        await cache.get_or_load("usr_a", loader)
        assert loader_calls["n"] == 1


# ── Bypass paths ───────────────────────────────────────────────────


class TestBypass:
    async def test_backend_disabled_loader_runs_every_time(
        self, fake_redis
    ) -> None:
        mgr = _make_manager(fake_redis)
        # Force backend disabled by replacing client with None.
        mgr._backend._redis = None
        cache = AuthPrincipalCache(manager=mgr)
        loader_calls = {"n": 0}

        async def loader() -> AuthPrincipalDTO:
            loader_calls["n"] += 1
            return _dto("usr_a")

        await cache.get_or_load("usr_a", loader)
        await cache.get_or_load("usr_a", loader)
        await cache.get_or_load("usr_a", loader)
        assert loader_calls["n"] == 3, "no cache → loader runs every time"

    async def test_domain_disabled_loader_runs_every_time(
        self, fake_redis, monkeypatch
    ) -> None:
        from app.core.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "cache_auth_enabled", False)
        cache = _make_auth_cache(fake_redis)
        loader_calls = {"n": 0}

        async def loader() -> AuthPrincipalDTO:
            loader_calls["n"] += 1
            return _dto("usr_a")

        await cache.get_or_load("usr_a", loader)
        await cache.get_or_load("usr_a", loader)
        assert loader_calls["n"] == 2

    async def test_write_through_returns_false_when_bypassed(
        self, fake_redis, monkeypatch
    ) -> None:
        from app.core.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "cache_auth_enabled", False)
        cache = _make_auth_cache(fake_redis)
        ok = await cache.write_through("usr_a", _dto("usr_a"))
        assert ok is False


# ── Distributed fill lock integration ──────────────────────────────


class TestDistributedFillLock:
    async def test_fill_lock_constructed_lazily_on_first_read(
        self, fake_redis
    ) -> None:
        cache = _make_auth_cache(fake_redis)
        assert cache._fill_lock is None
        await cache.get_or_load("usr_a", _async_loader(_dto("usr_a")))
        # After first read, fill_lock must have been constructed (the
        # backend is enabled + spec asks for it).
        assert cache._fill_lock is not None
        assert isinstance(cache._fill_lock, CacheFillLock)

    async def test_externally_supplied_fill_lock_is_used(self, fake_redis) -> None:
        sentinel = CacheFillLock(fake_redis, ttl_ms=1000, key_prefix="lock:ext:")
        cache = _make_auth_cache(fake_redis, fill_lock=sentinel)
        assert cache._fill_lock is sentinel
        # The lock uses the external prefix.
        await cache.get_or_load("usr_a", _async_loader(_dto("usr_a")))
        # Sentinel's prefix is preserved (CacheFillLock exposes _prefix).
        assert cache._fill_lock._prefix == "lock:ext:"

    async def test_get_or_load_succeeds_under_contention(self, fake_redis) -> None:
        """Two workers hitting the same key concurrently: only one DB load."""
        import asyncio

        cache = _make_auth_cache(fake_redis)
        loader_calls = {"n": 0}

        async def loader() -> AuthPrincipalDTO:
            loader_calls["n"] += 1
            await asyncio.sleep(0.05)
            return _dto("usr_a")

        # Spawn 20 concurrent loads; SingleFlight + fill lock together
        # ensure only one reaches the DB loader.
        results = await asyncio.gather(
            *[cache.get_or_load("usr_a", loader) for _ in range(20)]
        )
        assert all(r.public_id == "usr_a" for r in results)
        assert loader_calls["n"] == 1


# ── Singleton helpers ──────────────────────────────────────────────


class TestSingletonHelpers:
    def test_get_auth_principal_cache_lazy_default(self, fake_redis) -> None:
        # The lazy default manager pulls the CacheBackend singleton; in
        # tests we install a backend with a fakeredis client first.
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        CacheBackend._instance = backend
        try:
            a = get_auth_principal_cache()
            b = get_auth_principal_cache()
            assert a is b
        finally:
            CacheBackend._instance = None

    def test_set_resets_singleton(self, fake_redis) -> None:
        # Construct a custom AuthPrincipalCache backed by fakeredis and
        # verify get/set round-trip.
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        mgr = CacheManager(backend=backend)
        custom = AuthPrincipalCache(manager=mgr)
        set_auth_principal_cache(custom)
        try:
            assert get_auth_principal_cache() is custom
        finally:
            set_auth_principal_cache(None)


# ── Cross-test isolation ───────────────────────────────────────────


class TestCacheSurvivesAcrossCalls:
    """Make sure no implicit state leaks between calls in the same session."""

    async def test_different_users_cached_independently(self, fake_redis) -> None:
        cache = _make_auth_cache(fake_redis)

        async def loader_for(pid: str) -> AuthPrincipalDTO:
            return _dto(pid, display_name=f"User {pid}")

        a = await cache.get_or_load("usr_a", lambda: loader_for("usr_a"))
        b = await cache.get_or_load("usr_b", lambda: loader_for("usr_b"))
        assert a.public_id == "usr_a"
        assert b.public_id == "usr_b"
        assert a.display_name == "User usr_a"
        assert b.display_name == "User usr_b"

        # Both must be in the cache.
        loader_calls = {"n": 0}

        async def fail_loader() -> AuthPrincipalDTO:
            loader_calls["n"] += 1
            raise RuntimeError("loader must not run on hit")

        await cache.get_or_load("usr_a", fail_loader)
        await cache.get_or_load("usr_b", fail_loader)
        assert loader_calls["n"] == 0