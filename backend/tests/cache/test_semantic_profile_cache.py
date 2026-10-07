"""Tests for ``app.cache.domains.semantic_profile_cache``.

Coverage (per prompt §27 / 设计文档 §16):
  * Spec invariants: status-specific TTLs (ready 10m / pending 500ms / failed 30s)
  * Key namespace: sem:profile:{file_public_id}
  * get_many batched MGET:
      - all hits → 0 DB calls
      - partial hits → batched loader for missing only
      - all misses → batched loader for all
      - loader returns None for missing → negative cache (anti-stampede)
  * write_through with status-specific TTL
  * invalidate
  * Domain disabled / backend disabled → bypass
  * Multi-worker consistency
"""

from __future__ import annotations

import pytest

from app.cache.backend import CacheBackend
from app.cache.bulkhead import DBBulkhead
from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
from app.cache.domains.semantic_profile_cache import (
    SEMANTIC_PROFILE_SPEC,
    FileSemanticProfileCache,
    SemanticProfileDTO,
    SemanticProfileSpec,
    get_semantic_profile_cache,
    semantic_profile_key,
    set_semantic_profile_cache,
)
from app.cache.manager import CacheManager
from app.cache.metrics import cache_metrics
from app.cache.singleflight import SingleFlight


def _dto(pid: str = "file_x", status: str = "ready", **overrides) -> SemanticProfileDTO:
    base = dict(
        file_public_id=pid,
        status=status,
        metadata={
            "summary": f"Test summary for {pid}",
            "document_kind": "spec",
            "confidence": 0.85,
        },
    )
    base.update(overrides)
    return SemanticProfileDTO(**base)


def _make_manager(fake_redis) -> CacheManager:
    backend = CacheBackend(redis_client=fake_redis)
    backend._healthy = True
    return CacheManager(
        backend=backend,
        breaker=CircuitBreaker(BreakerConfig(failure_threshold=5, open_seconds=0.05)),
        bulkhead=DBBulkhead(max_concurrency=2),
        singleflight=SingleFlight(),
        jitter_fn=lambda ratio: 1.0,
    )


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch) -> None:
    CacheBackend._instance = None
    cache_metrics.reset()
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "cache_sem_enabled", True)
    yield
    CacheBackend._instance = None


# ── Spec invariants ────────────────────────────────────────────────


class TestSpecInvariants:
    def test_ready_ttl_10m(self) -> None:
        """Per 设计文档 §16 — ready TTL 10m."""
        assert SEMANTIC_PROFILE_SPEC.ready_ttl_seconds == 10 * 60

    def test_pending_ttl_500ms(self) -> None:
        """pending TTL 500ms — 让 100ms 短轮询收敛."""
        assert SEMANTIC_PROFILE_SPEC.pending_ttl_ms == 500

    def test_negative_ttl_250ms(self) -> None:
        """negative TTL 250ms — 防 stampede."""
        assert SEMANTIC_PROFILE_SPEC.negative_ttl_ms == 250

    def test_failed_ttl_30s(self) -> None:
        """failed 是 terminal, 30s 短 TTL."""
        assert SEMANTIC_PROFILE_SPEC.failed_ttl_seconds == 30

    def test_domain_is_sem(self) -> None:
        assert SEMANTIC_PROFILE_SPEC.domain == "sem"


class TestKeyNamespace:
    def test_profile_key(self) -> None:
        k = semantic_profile_key("file_xyz")
        assert k.endswith(":sem:profile:file_xyz"), k


# ── get_many batched MGET ─────────────────────────────────────────


class TestGetMany:
    async def test_empty_keys(self, fake_redis) -> None:
        cache = FileSemanticProfileCache(manager=_make_manager(fake_redis))

        async def loader(missing):
            return {}

        r = await cache.get_many([], loader)
        assert r == {}

    async def test_all_hits_zero_db_calls(self, fake_redis) -> None:
        cache = FileSemanticProfileCache(manager=_make_manager(fake_redis))

        # Pre-populate cache for 3 files.
        for pid in ("file_a", "file_b", "file_c"):
            await cache.write_through(_dto(pid=pid, status="ready"))

        loader_calls = {"missing": []}

        async def loader(missing):
            loader_calls["missing"] = list(missing)
            return {pid: _dto(pid=pid) for pid in missing}

        result = await cache.get_many(["file_a", "file_b", "file_c"], loader)
        assert loader_calls["missing"] == [], "all hits must not invoke loader"
        assert set(result.keys()) == {"file_a", "file_b", "file_c"}
        for pid, dto in result.items():
            assert dto.status == "ready"
            assert dto.metadata["summary"] == f"Test summary for {pid}"

    async def test_partial_hits_only_load_missing(self, fake_redis) -> None:
        cache = FileSemanticProfileCache(manager=_make_manager(fake_redis))

        # Pre-populate cache for 2 of 3 files.
        await cache.write_through(_dto(pid="file_a", status="ready"))
        await cache.write_through(_dto(pid="file_b", status="ready"))

        loader_calls = {"missing": []}

        async def loader(missing):
            loader_calls["missing"] = list(missing)
            return {pid: _dto(pid=pid) for pid in missing}

        result = await cache.get_many(
            ["file_a", "file_b", "file_c"], loader,
        )
        assert loader_calls["missing"] == ["file_c"], (
            "loader must be called only with missing keys"
        )
        assert "file_a" in result and "file_b" in result and "file_c" in result

    async def test_all_misses(self, fake_redis) -> None:
        cache = FileSemanticProfileCache(manager=_make_manager(fake_redis))

        loader_calls = {"missing": []}

        async def loader(missing):
            loader_calls["missing"] = list(missing)
            return {pid: _dto(pid=pid) for pid in missing}

        result = await cache.get_many(
            ["file_x", "file_y"], loader,
        )
        assert set(loader_calls["missing"]) == {"file_x", "file_y"}
        assert "file_x" in result and "file_y" in result

    async def test_loader_returns_none_writes_negative(self, fake_redis) -> None:
        cache = FileSemanticProfileCache(manager=_make_manager(fake_redis))

        async def loader(missing):
            return {pid: None for pid in missing}

        result = await cache.get_many(["file_missing"], loader)
        assert result["file_missing"] is None
        # Negative cache written → next call's loader not invoked.
        loader_calls = {"n": 0}

        async def counting_loader(missing):
            loader_calls["n"] += 1
            return {}

        await cache.get_many(["file_missing"], counting_loader)
        assert loader_calls["n"] == 0

    async def test_100_concurrent_get_many_single_loader_call(
        self, fake_redis
    ) -> None:
        """100 并发 get_many 同 key 集合 → SingleFlight dedupe."""
        import asyncio

        cache = FileSemanticProfileCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader(missing):
            loader_calls["n"] += 1
            await asyncio.sleep(0.01)
            return {pid: _dto(pid=pid) for pid in missing}

        results = await asyncio.gather(
            *[cache.get_many(["file_a", "file_b"], loader) for _ in range(100)]
        )
        # SingleFlight across concurrent get_many → 1 loader call.
        assert loader_calls["n"] == 1
        assert all("file_a" in r and "file_b" in r for r in results)


# ── write_through ─────────────────────────────────────────────────


class TestWriteThrough:
    async def test_write_ready_uses_long_ttl(self, fake_redis) -> None:
        cache = FileSemanticProfileCache(manager=_make_manager(fake_redis))
        ok = await cache.write_through(_dto(status="ready"))
        assert ok is True
        ttl = await fake_redis.ttl(f"ta:{_env()}:cache:v1:sem:profile:file_x")
        assert 590 <= ttl <= 610, ttl  # ~10m

    async def test_write_failed_uses_short_ttl(self, fake_redis) -> None:
        cache = FileSemanticProfileCache(manager=_make_manager(fake_redis))
        ok = await cache.write_through(_dto(status="failed"))
        assert ok is True
        ttl = await fake_redis.ttl(f"ta:{_env()}:cache:v1:sem:profile:file_x")
        assert 25 <= ttl <= 35, ttl

    async def test_write_pending_uses_sub_second_ttl(self, fake_redis) -> None:
        cache = FileSemanticProfileCache(manager=_make_manager(fake_redis))
        ok = await cache.write_through(_dto(status="pending"))
        assert ok is True
        # Sub-second TTL: fakeredis rounds to 0 or 1 sec.
        ttl = await fake_redis.ttl(f"ta:{_env()}:cache:v1:sem:profile:file_x")
        assert ttl <= 1, ttl

    async def test_invalidate_removes_entry(self, fake_redis) -> None:
        cache = FileSemanticProfileCache(manager=_make_manager(fake_redis))
        await cache.write_through(_dto(status="ready"))
        ok = await cache.invalidate("file_x")
        assert ok is True


def _env() -> str:
    from app.core.config import get_settings

    raw = get_settings().app_env or "dev"
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in raw)[:32] or "dev"


# ── Bypass paths ─────────────────────────────────────────────────────────


class TestBypass:
    async def test_domain_disabled_bypasses(self, fake_redis, monkeypatch) -> None:
        from app.core.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "cache_sem_enabled", False)
        cache = FileSemanticProfileCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader(missing):
            loader_calls["n"] += 1
            return {pid: _dto(pid=pid) for pid in missing}

        await cache.get_many(["file_a", "file_b"], loader)
        await cache.get_many(["file_a", "file_b"], loader)
        assert loader_calls["n"] == 2

    async def test_backend_disabled_bypasses(self) -> None:
        cache = FileSemanticProfileCache(
            manager=CacheManager(backend=CacheBackend(redis_client=None)),
        )
        loader_calls = {"n": 0}

        async def loader(missing):
            loader_calls["n"] += 1
            return {}

        await cache.get_many(["file_a"], loader)
        assert loader_calls["n"] == 1


# ── Multi-worker consistency ──────────────────────────────────────


class TestMultiWorker:
    async def test_worker_a_writes_worker_b_reads(self, fake_redis_pair) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = FileSemanticProfileCache(manager=_make_manager(worker_a_redis))
        cache_b = FileSemanticProfileCache(manager=_make_manager(worker_b_redis))

        # Worker A writes ready profile.
        ok = await cache_a.write_through(_dto(status="ready"))
        assert ok is True

        # Worker B reads.
        loader_calls = {"n": 0}

        async def stale_loader(missing):
            loader_calls["n"] += 1
            return {}

        result = await cache_b.get_many(["file_x"], stale_loader)
        assert loader_calls["n"] == 0, "Worker B should observe Worker A's write"
        assert "file_x" in result
        assert result["file_x"].status == "ready"

    async def test_batched_get_many_cross_worker(self, fake_redis_pair) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = FileSemanticProfileCache(manager=_make_manager(worker_a_redis))
        cache_b = FileSemanticProfileCache(manager=_make_manager(worker_b_redis))

        # Worker A writes 3 files.
        for pid in ("file_a", "file_b", "file_c"):
            await cache_a.write_through(_dto(pid=pid))

        # Worker B reads 3 files in one batched MGET.
        loader_calls = {"n": 0}

        async def stale_loader(missing):
            loader_calls["n"] += 1
            return {}

        result = await cache_b.get_many(
            ["file_a", "file_b", "file_c"], stale_loader,
        )
        assert loader_calls["n"] == 0, (
            "Batched MGET must avoid DB calls on warm cache"
        )
        assert set(result.keys()) == {"file_a", "file_b", "file_c"}


# ── Singleton helpers ─────────────────────────────────────────────


class TestSingletons:
    def test_get_semantic_profile_cache_lazy_default(self, fake_redis) -> None:
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        CacheBackend._instance = backend
        try:
            a = get_semantic_profile_cache()
            b = get_semantic_profile_cache()
            assert a is b
        finally:
            CacheBackend._instance = None

    def test_set_resets_singleton(self, fake_redis) -> None:
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        mgr = CacheManager(backend=backend)
        custom = FileSemanticProfileCache(manager=mgr)
        set_semantic_profile_cache(custom)
        try:
            assert get_semantic_profile_cache() is custom
        finally:
            set_semantic_profile_cache(None)