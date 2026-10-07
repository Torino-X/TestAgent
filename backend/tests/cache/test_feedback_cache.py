"""Tests for ``app.cache.domains.feedback_cache``.

Coverage (Step 9 — Business Cache Redis 迁移):
  * Spec invariants: TTL 24h, negative 60s, domain=fb
  * Key namespace: ta:{env}:cache:v1:fb:user:{user_id}:msg:{message_id}
  * DTO round-trip: feedback_type 正确序列化/反序列化
  * set/get/delete round-trip via CacheManager + fakeredis
  * "none" payload: 不 DEL key,保持 hit-rate 稳定
  * Disabled backend → bypass (返回 None / False)
  * Disabled domain flag (cache_fb_enabled=False) → bypass
  * Redis 异常 → fail-soft (返回 None / False, 不 raise)
  * Multi-worker: write one / read another
  * Singleton helpers (get_feedback_cache / set_feedback_cache)
"""

from __future__ import annotations

import pytest
from fakeredis import aioredis as fakeredis_aioredis

from app.cache.backend import CacheBackend
from app.cache.bulkhead import DBBulkhead
from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
from app.cache.domains.feedback_cache import (
    FEEDBACK_SPEC,
    FeedbackCache,
    FeedbackCacheDTO,
    feedback_key,
    get_feedback_cache,
    set_feedback_cache,
)
from app.cache.manager import CacheManager
from app.cache.metrics import cache_metrics
from app.cache.singleflight import SingleFlight


def _dto(ft: str = "like") -> FeedbackCacheDTO:
    return FeedbackCacheDTO(feedback_type=ft)


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


def _env() -> str:
    from app.core.config import get_settings

    raw = get_settings().app_env or "dev"
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in raw)[:32] or "dev"


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch) -> None:
    CacheBackend._instance = None
    cache_metrics.reset()
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "cache_fb_enabled", True)
    yield
    CacheBackend._instance = None


# ── Spec invariants ────────────────────────────────────────────────


class TestSpecInvariants:
    def test_ttl_24h(self) -> None:
        """Per 设计文档 §11.5 — TTL 24h."""
        assert FEEDBACK_SPEC.ttl_seconds == 24 * 3600
        assert FEEDBACK_SPEC.negative_ttl_seconds == 60

    def test_domain_is_fb(self) -> None:
        assert FEEDBACK_SPEC.domain == "fb"


class TestKeyNamespace:
    def test_key_format(self) -> None:
        k = feedback_key(42, 99)
        assert k.endswith(":fb:user:42:msg:99"), k
        # 完整前缀: ta:{env}:cache:v1:fb:user:42:msg:99
        assert k == f"ta:{_env()}:cache:v1:fb:user:42:msg:99", k


# ── DTO round-trip ────────────────────────────────────────────────


class TestDTORoundTrip:
    def test_to_from_dict_like(self) -> None:
        d = _dto("like")
        d2 = FeedbackCacheDTO.from_dict(d.to_dict())
        assert d2.feedback_type == "like"
        assert d2.value == "like"

    def test_to_from_dict_dislike(self) -> None:
        d = _dto("dislike")
        d2 = FeedbackCacheDTO.from_dict(d.to_dict())
        assert d2.feedback_type == "dislike"
        assert d2.value == "dislike"

    def test_to_from_dict_none(self) -> None:
        """``"none"`` 表示 cleared;``value`` 应返回 None (向后兼容)."""
        d = _dto("none")
        d2 = FeedbackCacheDTO.from_dict(d.to_dict())
        assert d2.feedback_type == "none"
        assert d2.value is None  # backward-compat

    def test_invalid_feedback_type_normalized_to_none(self) -> None:
        """未知 feedback_type (例如旧数据损坏) → 归一化为 "none"."""
        d2 = FeedbackCacheDTO.from_dict({"feedback_type": "invalid_xyz"})
        assert d2.feedback_type == "none"

    def test_missing_field_defaults_to_none(self) -> None:
        d2 = FeedbackCacheDTO.from_dict({})
        assert d2.feedback_type == "none"


# ── Round-trip via fakeredis ───────────────────────────────────────


class TestRoundTrip:
    async def test_set_then_get_like(self, fake_redis) -> None:
        cache = FeedbackCache(manager=_make_manager(fake_redis))
        ok = await cache.set(42, 99, "like")
        assert ok is True
        result = await cache.get(42, 99)
        assert result is not None
        assert result.feedback_type == "like"

    async def test_set_then_get_dislike(self, fake_redis) -> None:
        cache = FeedbackCache(manager=_make_manager(fake_redis))
        await cache.set(42, 99, "dislike")
        result = await cache.get(42, 99)
        assert result is not None
        assert result.feedback_type == "dislike"

    async def test_set_none_writes_none_payload(self, fake_redis) -> None:
        """``None`` 输入 → 写入 "none" (不 DEL, 保持 hit-rate 稳定)."""
        cache = FeedbackCache(manager=_make_manager(fake_redis))
        ok = await cache.set(42, 99, None)
        assert ok is True
        result = await cache.get(42, 99)
        assert result is not None
        assert result.feedback_type == "none"
        # Key 仍在 (没被 DEL)
        ttl = await fake_redis.ttl(feedback_key(42, 99))
        assert ttl > 0, "key 必须存在"

    async def test_set_invalid_type_rejected(self, fake_redis) -> None:
        cache = FeedbackCache(manager=_make_manager(fake_redis))
        ok = await cache.set(42, 99, "invalid_type")
        assert ok is False
        # 没有写入
        result = await cache.get(42, 99)
        assert result is None

    async def test_get_returns_none_on_miss(self, fake_redis) -> None:
        cache = FeedbackCache(manager=_make_manager(fake_redis))
        result = await cache.get(42, 99)
        assert result is None

    async def test_delete_removes_entry(self, fake_redis) -> None:
        cache = FeedbackCache(manager=_make_manager(fake_redis))
        await cache.set(42, 99, "like")
        ok = await cache.delete(42, 99)
        assert ok is True
        result = await cache.get(42, 99)
        assert result is None


# ── TTL 验证 ───────────────────────────────────────────────────────


class TestTTL:
    async def test_24h_ttl_applied(self, fake_redis) -> None:
        cache = FeedbackCache(manager=_make_manager(fake_redis))
        await cache.set(42, 99, "like")
        ttl = await fake_redis.ttl(feedback_key(42, 99))
        # fakeredis ttl 是秒; jitter ±10% → 86400 * 0.9 ~ 86400 * 1.1
        # 留 ±60s slack
        assert 86340 <= ttl <= 86460, f"ttl={ttl}, 期望 ~86400s (24h)"


# ── Bypass 路径 ────────────────────────────────────────────────────


class TestBypass:
    async def test_domain_disabled_bypasses(self, fake_redis, monkeypatch) -> None:
        from app.core.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "cache_fb_enabled", False)
        cache = FeedbackCache(manager=_make_manager(fake_redis))
        # 全部 bypass, 不写 Redis
        ok = await cache.set(42, 99, "like")
        assert ok is False
        result = await cache.get(42, 99)
        assert result is None

    async def test_backend_disabled_bypasses(self) -> None:
        cache = FeedbackCache(
            manager=CacheManager(backend=CacheBackend(redis_client=None)),
        )
        ok = await cache.set(42, 99, "like")
        assert ok is False
        result = await cache.get(42, 99)
        assert result is None


# ── Fail-soft 路径 ─────────────────────────────────────────────────


class TestFailSoft:
    async def test_redis_get_exception_returns_none(self) -> None:
        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("redis down")

            async def set(self, *a, **kw):
                raise ConnectionError("redis down")

            async def delete(self, *a, **kw):
                raise ConnectionError("redis down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        cache = FeedbackCache(manager=_make_manager(backend._redis))
        # 不 raise
        assert (await cache.get(42, 99)) is None
        assert (await cache.set(42, 99, "like")) is False
        assert (await cache.delete(42, 99)) is False

    async def test_corrupt_payload_returns_none(self, fake_redis) -> None:
        """手工塞 corrupt payload (绕过 set) → get 应返回 None 不 raise."""
        cache = FeedbackCache(manager=_make_manager(fake_redis))
        # 直接写入 raw bytes 不是 JSON envelope
        await fake_redis.set(feedback_key(42, 99), b"not-a-valid-envelope")
        result = await cache.get(42, 99)
        assert result is None


# ── Multi-worker 一致性 ────────────────────────────────────────────


class TestMultiWorker:
    async def test_worker_a_writes_worker_b_reads(self, fake_redis_pair) -> None:
        """Worker A 写 feedback → Worker B 立即读到 (跨进程/跨 worker 一致)."""
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = FeedbackCache(manager=_make_manager(worker_a_redis))
        cache_b = FeedbackCache(manager=_make_manager(worker_b_redis))

        # Worker A writes
        ok = await cache_a.set(42, 99, "like")
        assert ok is True

        # Worker B reads (no loader — direct get)
        result = await cache_b.get(42, 99)
        assert result is not None
        assert result.feedback_type == "like"

    async def test_worker_a_deletes_worker_b_misses(self, fake_redis_pair) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = FeedbackCache(manager=_make_manager(worker_a_redis))
        cache_b = FeedbackCache(manager=_make_manager(worker_b_redis))

        await cache_a.set(42, 99, "like")
        await cache_a.delete(42, 99)

        # Worker B reads → 应当返回 None
        result = await cache_b.get(42, 99)
        assert result is None


# ── Singleton helpers ─────────────────────────────────────────────


class TestSingletons:
    def test_get_feedback_cache_lazy_default(self, fake_redis) -> None:
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        CacheBackend._instance = backend
        try:
            a = get_feedback_cache()
            b = get_feedback_cache()
            assert a is b
        finally:
            CacheBackend._instance = None

    def test_set_resets_singleton(self, fake_redis) -> None:
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        mgr = CacheManager(backend=backend)
        custom = FeedbackCache(manager=mgr)
        set_feedback_cache(custom)
        try:
            assert get_feedback_cache() is custom
        finally:
            set_feedback_cache(None)
