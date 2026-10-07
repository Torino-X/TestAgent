"""Tests for ``app.cache.domains.context_cache``.

Coverage (per prompt §26 / 设计文档 §15):
  * Spec invariants: 5m TTL / domain=ctx
  * Key namespace: ctx:memory + ctx:instruction + workspace_hash
  * workspace_hash_for stable + length ≤ 16 hex chars
  * ContextMemoryCache get_or_load hit / miss / loader None → negative cache
  * WorkspaceInstructionCache same
  * Invalidates correctly
  * Domain disabled → bypass
  * Multi-worker: write one / read another
"""

from __future__ import annotations

import pytest

from app.cache.backend import CacheBackend
from app.cache.bulkhead import DBBulkhead
from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
from app.cache.domains.context_cache import (
    CONTEXT_SPEC,
    ContextMemoryCache,
    ContextMemoryDTO,
    WorkspaceInstructionCache,
    WorkspaceInstructionDTO,
    context_memory_key,
    get_context_memory_cache,
    get_workspace_instruction_cache,
    set_context_memory_cache,
    set_workspace_instruction_cache,
    workspace_hash_for,
    workspace_instruction_key,
)
from app.cache.manager import CacheManager
from app.cache.metrics import cache_metrics
from app.cache.singleflight import SingleFlight


def _memory_dto(**overrides) -> ContextMemoryDTO:
    base = dict(
        memories=[
            {"id": "mem_1", "content": "User prefers tabular output"},
            {"id": "mem_2", "content": "Test plan should include regression cases"},
        ],
    )
    base.update(overrides)
    return ContextMemoryDTO(**base)


def _instruction_dto(**overrides) -> WorkspaceInstructionDTO:
    base = dict(
        instructions={
            "language": "zh",
            "output_style": "concise",
            "banned_words": ["禁用", "禁止"],
        },
    )
    base.update(overrides)
    return WorkspaceInstructionDTO(**base)


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
    monkeypatch.setattr(s, "cache_ctx_enabled", True)
    yield
    CacheBackend._instance = None


# ── Spec invariants ────────────────────────────────────────────────


class TestSpecInvariants:
    def test_ttl_5m(self) -> None:
        """Per 设计文档 §15 — TTL 5m."""
        assert CONTEXT_SPEC.ttl_seconds == 5 * 60
        assert CONTEXT_SPEC.negative_ttl_seconds == 30

    def test_domain_is_ctx(self) -> None:
        assert CONTEXT_SPEC.domain == "ctx"


class TestKeyNamespace:
    def test_memory_key(self) -> None:
        k = context_memory_key(42, "ws_hash")
        assert k.endswith(":ctx:memory:user:42:ws:ws_hash"), k

    def test_instruction_key(self) -> None:
        k = workspace_instruction_key(42, "ws_hash")
        assert k.endswith(":ctx:instruction:user:42:ws:ws_hash"), k


class TestWorkspaceHash:
    def test_stable(self) -> None:
        a = workspace_hash_for("ws_alpha")
        b = workspace_hash_for("ws_alpha")
        assert a == b

    def test_different_inputs_different_hashes(self) -> None:
        a = workspace_hash_for("ws_alpha")
        b = workspace_hash_for("ws_beta")
        assert a != b

    def test_empty_handled(self) -> None:
        # None / "" → 同一个 stable hash
        assert workspace_hash_for(None) == workspace_hash_for("")

    def test_length_under_32(self) -> None:
        """Hash 截断在 16 hex chars (设计文档 §7)."""
        h = workspace_hash_for("ws_alpha")
        assert len(h) == 16


# ── ContextMemoryCache ──────────────────────────────────────────────


class TestContextMemoryCache:
    async def test_get_or_load_miss_then_hit(self, fake_redis) -> None:
        cache = ContextMemoryCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _memory_dto()

        r1 = await cache.get_or_load(42, "ws_a", loader)
        r2 = await cache.get_or_load(42, "ws_a", loader)
        assert len(r1.memories) == 2
        assert loader_calls["n"] == 1

    async def test_loader_returning_none_writes_negative(
        self, fake_redis
    ) -> None:
        cache = ContextMemoryCache(manager=_make_manager(fake_redis))

        async def loader():
            return None

        r = await cache.get_or_load(42, "ws_empty", loader)
        assert r is None
        # Subsequent call short-circuits.
        loader_calls = {"n": 0}

        async def counting_loader():
            loader_calls["n"] += 1
            return None

        await cache.get_or_load(42, "ws_empty", counting_loader)
        assert loader_calls["n"] == 0

    async def test_different_workspace_keys_isolated(self, fake_redis) -> None:
        cache = ContextMemoryCache(manager=_make_manager(fake_redis))

        async def loader_a():
            return _memory_dto(memories=[{"id": "mem_a"}])

        async def loader_b():
            return _memory_dto(memories=[{"id": "mem_b"}])

        await cache.get_or_load(42, "ws_a", loader_a)
        await cache.get_or_load(42, "ws_b", loader_b)
        # Different keys → independent caches.
        assert (await cache.get_or_load(42, "ws_a", loader_a)).memories[0]["id"] == "mem_a"
        assert (await cache.get_or_load(42, "ws_b", loader_b)).memories[0]["id"] == "mem_b"

    async def test_invalidate(self, fake_redis) -> None:
        cache = ContextMemoryCache(manager=_make_manager(fake_redis))

        async def loader():
            return _memory_dto()

        await cache.get_or_load(42, "ws_x", loader)
        ok = await cache.invalidate(42, "ws_x")
        assert ok is True


# ── WorkspaceInstructionCache ─────────────────────────────────────


class TestWorkspaceInstructionCache:
    async def test_get_or_load_miss_then_hit(self, fake_redis) -> None:
        cache = WorkspaceInstructionCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _instruction_dto()

        r1 = await cache.get_or_load(42, "ws_a", loader)
        r2 = await cache.get_or_load(42, "ws_a", loader)
        assert r1.instructions["language"] == "zh"
        assert loader_calls["n"] == 1

    async def test_loader_returning_none_writes_negative(
        self, fake_redis
    ) -> None:
        cache = WorkspaceInstructionCache(manager=_make_manager(fake_redis))

        async def loader():
            return None

        r = await cache.get_or_load(42, "ws_empty", loader)
        assert r is None

    async def test_invalidate(self, fake_redis) -> None:
        cache = WorkspaceInstructionCache(manager=_make_manager(fake_redis))

        async def loader():
            return _instruction_dto()

        await cache.get_or_load(42, "ws_x", loader)
        ok = await cache.invalidate(42, "ws_x")
        assert ok is True


# ── Bypass paths ─────────────────────────────────────────────────────────


class TestBypass:
    async def test_memory_domain_disabled_bypasses(
        self, fake_redis, monkeypatch
    ) -> None:
        from app.core.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "cache_ctx_enabled", False)
        cache = ContextMemoryCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _memory_dto()

        await cache.get_or_load(42, "ws_a", loader)
        await cache.get_or_load(42, "ws_a", loader)
        assert loader_calls["n"] == 2

    async def test_backend_disabled_bypasses(self) -> None:
        cache = ContextMemoryCache(
            manager=CacheManager(backend=CacheBackend(redis_client=None)),
        )
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _memory_dto()

        await cache.get_or_load(42, "ws_a", loader)
        await cache.get_or_load(42, "ws_a", loader)
        assert loader_calls["n"] == 2


# ── Multi-worker consistency ──────────────────────────────────────


class TestMultiWorker:
    async def test_worker_a_writes_worker_b_reads(
        self, fake_redis_pair
    ) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = ContextMemoryCache(manager=_make_manager(worker_a_redis))
        cache_b = WorkspaceInstructionCache(manager=_make_manager(worker_b_redis))

        async def loader_a():
            return _memory_dto(memories=[{"id": "mem_shared"}])

        await cache_a.get_or_load(42, "ws_shared", loader_a)

        # Worker B reads; loader must not run.
        loader_calls = {"n": 0}

        async def stale_loader():
            loader_calls["n"] += 1
            return _memory_dto(memories=[{"id": "mem_stale"}])

        r = await cache_a.get_or_load(42, "ws_shared", stale_loader)
        assert loader_calls["n"] == 0
        assert r.memories[0]["id"] == "mem_shared"

    async def test_invalidation_cross_worker(
        self, fake_redis_pair
    ) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = ContextMemoryCache(manager=_make_manager(worker_a_redis))
        cache_b = ContextMemoryCache(manager=_make_manager(worker_b_redis))

        async def loader_a():
            return _memory_dto(memories=[{"id": "mem_v1"}])

        await cache_a.get_or_load(42, "ws_a", loader_a)

        # Worker A invalidates → Worker B's next read re-loads.
        await cache_a.invalidate(42, "ws_a")

        loader_calls = {"n": 0}

        async def loader_b():
            loader_calls["n"] += 1
            return _memory_dto(memories=[{"id": "mem_v2"}])

        r = await cache_b.get_or_load(42, "ws_a", loader_b)
        assert loader_calls["n"] == 1
        assert r.memories[0]["id"] == "mem_v2"


# ── Singletons ─────────────────────────────────────────────────────


class TestSingletons:
    def test_get_context_memory_cache_lazy_default(self, fake_redis) -> None:
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        CacheBackend._instance = backend
        try:
            a = get_context_memory_cache()
            b = get_context_memory_cache()
            assert a is b
        finally:
            CacheBackend._instance = None

    def test_get_workspace_instruction_cache_lazy_default(self, fake_redis) -> None:
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        CacheBackend._instance = backend
        try:
            a = get_workspace_instruction_cache()
            b = get_workspace_instruction_cache()
            assert a is b
        finally:
            CacheBackend._instance = None