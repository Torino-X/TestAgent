"""Tests for ``app.cache.domains.conversation_cache``.

Coverage (per prompt §7, §21, §27 / 设计文档 §12):
  * Spec invariants: list 60s / detail 60s / negative 30s
  * Key namespace format
  * Generation token lifecycle (init / persist / rotate)
  * bump_generation invalidates old list keys (no scan required)
  * list cache-aside: miss → hit, 100 concurrent → 1 loader
  * detail cache-aside: miss → hit
  * negative-cache: empty list short-circuits
  * Domain disabled / backend disabled → bypass
  * Multi-worker: worker A bumps, worker B sees new token on read
"""

from __future__ import annotations

import pytest

from app.cache.backend import CacheBackend
from app.cache.bulkhead import DBBulkhead
from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
from app.cache.domains.conversation_cache import (
    CONVERSATION_DETAIL_SPEC,
    CONVERSATION_LIST_SPEC,
    ConversationCache,
    ConversationDetailDTO,
    ConversationListDTO,
    conv_detail_key,
    conv_generation_key,
    conv_list_key,
    get_conversation_cache,
    set_conversation_cache,
)
from app.cache.manager import CacheManager
from app.cache.metrics import cache_metrics
from app.cache.singleflight import SingleFlight


# ── helpers ────────────────────────────────────────────────────────


def _list_dto(uid: int, *, total: int = 0, conv_id: str | None = None) -> ConversationListDTO:
    summaries = (
        [
            {
                "id": conv_id or f"conv_{uid}",
                "title": f"Test conv {uid}",
                "state": "active",
                "status": "active",
                "updated_at": "2026-09-06T12:00:00",
                "message_count": 0,
                "file_count": 0,
            }
        ]
        if conv_id or uid
        else []
    )
    return ConversationListDTO(summaries=summaries, total=total or len(summaries))


def _detail_dto(conv_public_id: str = "conv_xyz") -> ConversationDetailDTO:
    return ConversationDetailDTO(
        payload={
            "id": conv_public_id,
            "title": "Test conv",
            "state": "active",
            "status": "active",
            "updated_at": "2026-09-06T12:00:00",
            "message_count": 0,
            "file_count": 0,
            "files": [],
            "messages": [],
            "latest_task": None,
        },
    )


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
    monkeypatch.setattr(s, "cache_conv_enabled", True)
    yield
    CacheBackend._instance = None


# ── Spec invariants ────────────────────────────────────────────────


class TestSpecInvariants:
    def test_list_ttl_60s(self) -> None:
        """Per 设计文档 §12.4 — list TTL 60s."""
        assert CONVERSATION_LIST_SPEC.ttl_seconds == 60
        assert CONVERSATION_LIST_SPEC.negative_ttl_seconds == 30

    def test_detail_ttl_60s(self) -> None:
        assert CONVERSATION_DETAIL_SPEC.ttl_seconds == 60

    def test_domain_is_conv(self) -> None:
        assert CONVERSATION_LIST_SPEC.domain == "conv"
        assert CONVERSATION_DETAIL_SPEC.domain == "conv"


class TestKeyNamespace:
    def test_list_key(self) -> None:
        k = conv_list_key(42, "gen123", "all")
        assert k.endswith(":conv:list:user:42:g:gen123:q:all"), k

    def test_detail_key(self) -> None:
        k = conv_detail_key(42, "conv_xyz")
        assert k.endswith(":conv:detail:user:42:conv:conv_xyz"), k

    def test_generation_key(self) -> None:
        k = conv_generation_key(42)
        assert k.endswith(":gen:conv:user:42"), k


# ── Generation token lifecycle ─────────────────────────────────────


class TestGenerationToken:
    async def test_get_initializes_if_missing(self, fake_redis) -> None:
        cache = ConversationCache(manager=_make_manager(fake_redis))
        gen1 = await cache.get_generation(42)
        assert gen1, "first read must produce a token"
        gen2 = await cache.get_generation(42)
        assert gen1 == gen2, "second read must observe the same token"

    async def test_bump_rotates_token(self, fake_redis) -> None:
        cache = ConversationCache(manager=_make_manager(fake_redis))
        gen1 = await cache.get_generation(42)
        gen2 = await cache.bump_generation(42)
        assert gen1 != gen2, "bump must change the token"
        # Subsequent get returns the bumped token.
        gen3 = await cache.get_generation(42)
        assert gen2 == gen3

    async def test_independent_users(self, fake_redis) -> None:
        cache = ConversationCache(manager=_make_manager(fake_redis))
        await cache.bump_generation(42)
        await cache.bump_generation(43)
        assert (
            await cache.get_generation(42)
            != await cache.get_generation(43)
        )

    async def test_bump_invalidates_old_list_keys(self, fake_redis) -> None:
        """Key invariant: bumping gen must change the list cache Key.

        Old list cache entries become unreachable without needing to
        scan / DEL them (设计文档 §12.3).
        """
        cache = ConversationCache(manager=_make_manager(fake_redis))

        # Initial gen + populate list cache.
        gen1 = await cache.get_generation(42)

        async def loader():
            return _list_dto(42, total=1, conv_id="conv_initial")

        r1 = await cache.get_or_load_list(42, loader)
        assert r1.summaries[0]["id"] == "conv_initial"

        # Bump gen.
        gen2 = await cache.bump_generation(42)
        assert gen1 != gen2

        # Next list read: loader runs again (cache key different).
        loader_calls = {"n": 0}

        async def counting_loader():
            loader_calls["n"] += 1
            return _list_dto(42, total=1, conv_id="conv_after_bump")

        r2 = await cache.get_or_load_list(42, counting_loader)
        assert loader_calls["n"] == 1
        assert r2.summaries[0]["id"] == "conv_after_bump"


# ── list cache-aside ──────────────────────────────────────────────


class TestListCacheAside:
    async def test_miss_then_hit(self, fake_redis) -> None:
        cache = ConversationCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _list_dto(42, total=1, conv_id="conv_xyz")

        r1 = await cache.get_or_load_list(42, loader)
        r2 = await cache.get_or_load_list(42, loader)
        assert len(r1.summaries) == 1
        assert len(r2.summaries) == 1
        assert loader_calls["n"] == 1, "second call must hit cache"

    async def test_100_concurrent_misses_coalesce(self, fake_redis) -> None:
        import asyncio

        cache = ConversationCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            await asyncio.sleep(0.01)
            return _list_dto(42, total=3, conv_id="conv_a")

        results = await asyncio.gather(
            *[cache.get_or_load_list(42, loader) for _ in range(100)]
        )
        assert loader_calls["n"] == 1
        assert all(len(r.summaries) == 1 for r in results)

    async def test_empty_list_returns_empty_dto(self, fake_redis) -> None:
        cache = ConversationCache(manager=_make_manager(fake_redis))

        async def loader():
            return ConversationListDTO(summaries=[], total=0)

        r = await cache.get_or_load_list(42, loader)
        assert r.summaries == []
        assert r.total == 0
        # Second call: empty list cached (positive cache).
        loader_calls = {"n": 0}

        async def counting_loader():
            loader_calls["n"] += 1
            return ConversationListDTO(summaries=[], total=0)

        await cache.get_or_load_list(42, counting_loader)
        assert loader_calls["n"] == 0

    async def test_loader_returning_none_writes_negative(self, fake_redis) -> None:
        cache = ConversationCache(manager=_make_manager(fake_redis))

        async def loader():
            return None

        r = await cache.get_or_load_list(42, loader)
        assert r.summaries == []
        assert r.total == 0
        # Subsequent call short-circuits (negative cache).
        loader_calls = {"n": 0}

        async def counting_loader():
            loader_calls["n"] += 1
            return None

        await cache.get_or_load_list(42, counting_loader)
        assert loader_calls["n"] == 0


# ── detail cache-aside ─────────────────────────────────────────────


class TestDetailCacheAside:
    async def test_miss_then_hit(self, fake_redis) -> None:
        cache = ConversationCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _detail_dto("conv_xyz")

        r1 = await cache.get_or_load_detail(42, "conv_xyz", loader)
        r2 = await cache.get_or_load_detail(42, "conv_xyz", loader)
        assert r1.payload["title"] == "Test conv"
        assert loader_calls["n"] == 1

    async def test_loader_returning_none_writes_negative(self, fake_redis) -> None:
        cache = ConversationCache(manager=_make_manager(fake_redis))

        async def loader():
            return None

        r = await cache.get_or_load_detail(42, "missing", loader)
        assert r is None
        loader_calls = {"n": 0}

        async def counting_loader():
            loader_calls["n"] += 1
            return None

        await cache.get_or_load_detail(42, "missing", counting_loader)
        assert loader_calls["n"] == 0

    async def test_invalidate_detail_removes_entry(self, fake_redis) -> None:
        cache = ConversationCache(manager=_make_manager(fake_redis))

        async def loader():
            return _detail_dto()

        await cache.get_or_load_detail(42, "conv_xyz", loader)
        ok = await cache.invalidate_detail(42, "conv_xyz")
        assert ok is True


# ── Bypass paths ───────────────────────────────────────────────────


class TestBypass:
    async def test_domain_disabled_bypasses(self, fake_redis, monkeypatch) -> None:
        from app.core.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "cache_conv_enabled", False)
        cache = ConversationCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _list_dto(42)

        await cache.get_or_load_list(42, loader)
        await cache.get_or_load_list(42, loader)
        assert loader_calls["n"] == 2

    async def test_backend_disabled_bypasses(self) -> None:
        cache = ConversationCache(
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

        cache_a = ConversationCache(manager=_make_manager(worker_a_redis))
        cache_b = ConversationCache(manager=_make_manager(worker_b_redis))

        # Worker A bumps gen.
        gen1 = await cache_a.bump_generation(42)
        # Worker B sees the new token.
        gen2 = await cache_b.get_generation(42)
        assert gen1 == gen2, (
            "Generation token must be shared across workers (per design §12.2)"
        )

    async def test_worker_a_list_write_visible_to_worker_b(
        self, fake_redis_pair
    ) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = ConversationCache(manager=_make_manager(worker_a_redis))
        cache_b = ConversationCache(manager=_make_manager(worker_b_redis))

        # Worker A populates list cache.
        async def loader_a():
            return _list_dto(42, total=1, conv_id="conv_a")

        await cache_a.get_or_load_list(42, loader_a)

        # Worker B reads with loader returning DIFFERENT data — must hit
        # the shared cache and return Worker A's data, not invoke loader.
        loader_calls = {"n": 0}

        async def loader_b():
            loader_calls["n"] += 1
            return _list_dto(42, total=99, conv_id="conv_b")

        r = await cache_b.get_or_load_list(42, loader_b)
        assert loader_calls["n"] == 0
        assert r.summaries[0]["id"] == "conv_a"

    async def test_bump_makes_old_list_cache_unreachable(
        self, fake_redis_pair
    ) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = ConversationCache(manager=_make_manager(worker_a_redis))
        cache_b = ConversationCache(manager=_make_manager(worker_b_redis))

        # Both workers populate cache.
        async def initial_loader():
            return _list_dto(42, total=1, conv_id="conv_v1")

        await cache_a.get_or_load_list(42, initial_loader)
        await cache_b.get_or_load_list(42, initial_loader)

        # Worker A bumps.
        await cache_a.bump_generation(42)

        # Worker B's next read: must re-load (gen token changed → cache miss).
        loader_calls = {"n": 0}

        async def new_loader():
            loader_calls["n"] += 1
            return _list_dto(42, total=1, conv_id="conv_v2")

        r = await cache_b.get_or_load_list(42, new_loader)
        assert loader_calls["n"] == 1, "bump must invalidate Worker B's view"
        assert r.summaries[0]["id"] == "conv_v2"

    async def test_detail_cache_shared(self, fake_redis_pair) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = ConversationCache(manager=_make_manager(worker_a_redis))
        cache_b = ConversationCache(manager=_make_manager(worker_b_redis))

        async def loader_a():
            return _detail_dto("conv_x")

        await cache_a.get_or_load_detail(42, "conv_x", loader_a)

        loader_calls = {"n": 0}

        async def stale_loader():
            loader_calls["n"] += 1
            return _detail_dto("WRONG")

        r = await cache_b.get_or_load_detail(42, "conv_x", stale_loader)
        assert loader_calls["n"] == 0
        assert r.payload["id"] == "conv_x"


# ── Singletons ─────────────────────────────────────────────────────


class TestSingletons:
    def test_get_conversation_cache_lazy_default(self, fake_redis) -> None:
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        CacheBackend._instance = backend
        try:
            a = get_conversation_cache()
            b = get_conversation_cache()
            assert a is b
        finally:
            CacheBackend._instance = None

    def test_set_resets_singleton(self, fake_redis) -> None:
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        mgr = CacheManager(backend=backend)
        custom = ConversationCache(manager=mgr)
        set_conversation_cache(custom)
        try:
            assert get_conversation_cache() is custom
        finally:
            set_conversation_cache(None)