"""Tests for ``app.cache.domains.library_cache``.

Coverage (per prompt §25 / 设计文档 §14):
  * Spec invariants: list 60s / domain / fill_lock flag
  * Key namespace format
  * Generation token lifecycle (init / persist / rotate)
  * bump_generation invalidates old list keys
  * list cache-aside: miss → hit, 100 concurrent → 1 loader
  * filter_hash_for stable across ordering / different params
  * Domain disabled → bypass
  * Multi-worker: worker A bumps, worker B sees new token
"""

from __future__ import annotations

import pytest

from app.cache.backend import CacheBackend
from app.cache.bulkhead import DBBulkhead
from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
from app.cache.domains.library_cache import (
    GENERATION_SPEC,
    LIBRARY_LIST_SPEC,
    LibraryCache,
    LibraryListDTO,
    filter_hash_for,
    get_library_cache,
    library_generation_key,
    library_list_key,
    set_library_cache,
)
from app.cache.manager import CacheManager
from app.cache.metrics import cache_metrics
from app.cache.singleflight import SingleFlight


def _list_dto(uid: int = 42, *, total: int = 0, item_id: str | None = None) -> LibraryListDTO:
    items = (
        [
            {
                "id": item_id or f"file_{uid}",
                "name": f"item-{uid}",
                "kind": "file",
                "source": "upload",
                "mime_type": "application/pdf",
                "extension": ".pdf",
                "size_bytes": 1024,
                "modified_at": "2026-09-06T12:00:00",
                "download_url": f"/api/library/items/{item_id or 'x'}/download",
                "deleted_at": None,
            }
        ]
        if item_id or uid
        else []
    )
    return LibraryListDTO(items=items, total=total or len(items))


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
    monkeypatch.setattr(s, "cache_lib_enabled", True)
    yield
    CacheBackend._instance = None


# ── Spec invariants ────────────────────────────────────────────────


class TestSpecInvariants:
    def test_list_ttl_60s(self) -> None:
        """Per 设计文档 §14.4 — list TTL 60s."""
        assert LIBRARY_LIST_SPEC.ttl_seconds == 60
        assert LIBRARY_LIST_SPEC.negative_ttl_seconds == 30

    def test_domain_is_lib(self) -> None:
        assert LIBRARY_LIST_SPEC.domain == "lib"
        assert LIBRARY_LIST_SPEC.enable_distributed_fill_lock is True

    def test_generation_no_ttl_jitter(self) -> None:
        # Generation Key 不需要 jitter — token 长期稳定
        assert GENERATION_SPEC.jitter_ratio == 0.0


class TestKeyNamespace:
    def test_list_key(self) -> None:
        k = library_list_key(42, "gen123", "all")
        assert k.endswith(":lib:list:user:42:g:gen123:q:all"), k

    def test_generation_key(self) -> None:
        k = library_generation_key(42)
        assert k.endswith(":gen:lib:user:42"), k


class TestFilterHash:
    def test_deterministic(self) -> None:
        h1 = filter_hash_for(category="all", query="", scope="active", source="all", file_type="all")
        h2 = filter_hash_for(category="all", query="", scope="active", source="all", file_type="all")
        assert h1 == h2

    def test_different_filters_different_hashes(self) -> None:
        a = filter_hash_for(category="image")
        b = filter_hash_for(category="file")
        assert a != b

    def test_length_under_32(self) -> None:
        """Hash 截断在 16 hex chars (设计文档 §7)."""
        h = filter_hash_for()
        assert len(h) == 16


# ── Generation token lifecycle ─────────────────────────────────────


class TestGenerationToken:
    async def test_get_initializes_if_missing(self, fake_redis) -> None:
        cache = LibraryCache(manager=_make_manager(fake_redis))
        gen1 = await cache.get_generation(42)
        assert gen1
        gen2 = await cache.get_generation(42)
        assert gen1 == gen2

    async def test_bump_rotates_token(self, fake_redis) -> None:
        cache = LibraryCache(manager=_make_manager(fake_redis))
        gen1 = await cache.get_generation(42)
        gen2 = await cache.bump_generation(42)
        assert gen1 != gen2

    async def test_bump_invalidates_old_list_keys(self, fake_redis) -> None:
        cache = LibraryCache(manager=_make_manager(fake_redis))

        gen1 = await cache.get_generation(42)

        async def loader():
            return _list_dto(42, item_id="file_initial")

        r1 = await cache.get_or_load_list(42, loader)
        assert r1.items[0]["id"] == "file_initial"

        # Bump → 旧 list 不可达
        gen2 = await cache.bump_generation(42)
        assert gen1 != gen2

        loader_calls = {"n": 0}

        async def counting_loader():
            loader_calls["n"] += 1
            return _list_dto(42, item_id="file_after")

        r2 = await cache.get_or_load_list(42, counting_loader)
        assert loader_calls["n"] == 1
        assert r2.items[0]["id"] == "file_after"


# ── list cache-aside ──────────────────────────────────────────────


class TestListCacheAside:
    async def test_miss_then_hit(self, fake_redis) -> None:
        cache = LibraryCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _list_dto(42, item_id="file_xyz")

        r1 = await cache.get_or_load_list(42, loader)
        r2 = await cache.get_or_load_list(42, loader)
        assert len(r1.items) == 1
        assert loader_calls["n"] == 1

    async def test_100_concurrent_misses_coalesce(self, fake_redis) -> None:
        import asyncio

        cache = LibraryCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            await asyncio.sleep(0.01)
            return _list_dto(42, item_id="file_a")

        results = await asyncio.gather(
            *[cache.get_or_load_list(42, loader) for _ in range(100)]
        )
        assert loader_calls["n"] == 1
        assert all(len(r.items) == 1 for r in results)

    async def test_filter_hash_isolation(self, fake_redis) -> None:
        cache = LibraryCache(manager=_make_manager(fake_redis))

        async def loader():
            return _list_dto(42, item_id="file_a")

        # Different filter hashes → different cache keys.
        await cache.get_or_load_list(42, loader, filter_hash="all")
        loader_calls = {"n": 0}

        async def counting_loader():
            loader_calls["n"] += 1
            return _list_dto(42, item_id="file_b")

        await cache.get_or_load_list(42, counting_loader, filter_hash="image")
        # Different filter hash → loader runs again.
        assert loader_calls["n"] == 1


# ── Bypass paths ─────────────────────────────────────────────────────────


class TestBypass:
    async def test_domain_disabled_bypasses(self, fake_redis, monkeypatch) -> None:
        from app.core.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "cache_lib_enabled", False)
        cache = LibraryCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _list_dto(42)

        await cache.get_or_load_list(42, loader)
        await cache.get_or_load_list(42, loader)
        assert loader_calls["n"] == 2

    async def test_backend_disabled_bypasses(self) -> None:
        cache = LibraryCache(
            manager=CacheManager(backend=CacheBackend(redis_client=None)),
        )
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _list_dto(42)

        await cache.get_or_load_list(42, loader)
        await cache.get_or_load_list(42, loader)
        assert loader_calls["n"] == 2


# ── Multi-worker consistency ──────────────────────────────────────


class TestMultiWorker:
    async def test_worker_a_bumps_worker_b_reads_new_token(
        self, fake_redis_pair
    ) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = LibraryCache(manager=_make_manager(worker_a_redis))
        cache_b = LibraryCache(manager=_make_manager(worker_b_redis))

        gen1 = await cache_a.bump_generation(42)
        gen2 = await cache_b.get_generation(42)
        assert gen1 == gen2, "Generation token must be shared across workers"

    async def test_bump_makes_old_list_cache_unreachable(
        self, fake_redis_pair
    ) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = LibraryCache(manager=_make_manager(worker_a_redis))
        cache_b = LibraryCache(manager=_make_manager(worker_b_redis))

        async def initial_loader():
            return _list_dto(42, item_id="file_v1")

        await cache_a.get_or_load_list(42, initial_loader)

        # Worker A bumps → Worker B must re-load.
        await cache_a.bump_generation(42)

        loader_calls = {"n": 0}

        async def new_loader():
            loader_calls["n"] += 1
            return _list_dto(42, item_id="file_v2")

        r = await cache_b.get_or_load_list(42, new_loader)
        assert loader_calls["n"] == 1
        assert r.items[0]["id"] == "file_v2"


# ── Singleton helpers ─────────────────────────────────────────────


class TestSingletons:
    def test_get_library_cache_lazy_default(self, fake_redis) -> None:
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        CacheBackend._instance = backend
        try:
            a = get_library_cache()
            b = get_library_cache()
            assert a is b
        finally:
            CacheBackend._instance = None

    def test_set_resets_singleton(self, fake_redis) -> None:
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        mgr = CacheManager(backend=backend)
        custom = LibraryCache(manager=mgr)
        set_library_cache(custom)
        try:
            assert get_library_cache() is custom
        finally:
            set_library_cache(None)