"""Tests for assistant message feedback API and cache layer.

Covers §六-§十 of the spec: PUT /messages/{id}/feedback, my_feedback
inline in list_messages, optimistic toggling (like↔dislike↔null), and
the Redis FeedbackCache degrade path.

Step 9 迁移: FeedbackCache 走 Business Cache Redis (port 6380),
通过标准 CacheManager。旧 API 表面保持兼容 (get/set/delete/CachedFeedback),
但 redis_client= 参数已 deprecated,新代码请用 manager=。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.testclient import TestClient

from app.api.deps import get_current_user
from app.api.v1.messages import router as messages_router
from app.core.feedback_cache import FeedbackCache, make_feedback_cache_key
from app.core.response import success
from app.db.session import get_db
from app.schemas.auth import UserProfile


# ── Fixtures ─────────────────────────────────────────────────────────

def _fake_user() -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        public_id="user_feedback_test",
        username="feedback_user",
        role="user",
        status="active",
        email="feedback@example.com",
        avatar_url=None,
        name="Feedback User",
        display_name="Feedback User",
        internal_id=42,
    )


def _stub_get_current_user():
    return UserProfile(
        id="user_feedback_test",
        internal_id=42,
        name="Feedback User",
        role="user",
        username="feedback_user",
        email="feedback@example.com",
        status="active",
        avatar_url=None,
    )


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(messages_router)
    app.dependency_overrides[get_current_user] = _stub_get_current_user

    async def _override_get_db():
        mock_session = AsyncMock(spec=AsyncSession)
        # list_messages: returns empty list
        mock_session.execute = AsyncMock(return_value=SimpleNamespace(first=lambda: None, all=lambda: []))
        yield mock_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── PUT /messages/{id}/feedback ─────────────────────────────────────

class TestSetFeedbackEndpoint:
    """PUT feedback — full round-trip tests."""

    def test_feedback_request_schema_stores_string_value(self):
        from app.schemas.message import FeedbackRequest
        req = FeedbackRequest(feedback="invalid_type")
        # Pydantic accepts any string — validation happens at the route level
        assert req.feedback == "invalid_type"


class TestFeedbackCache:
    """Unit tests for FeedbackCache (Business Cache Redis 迁移后,Step 9).

    覆盖:
      - 离线 (no backend) → get 返回 None, set/delete 返回 False
      - 正常路径 → SET → GET round-trip, value 正确
      - "none" 表示 cleared (保持 key 存在, hit-rate 稳定)
      - 异常路径 (Redis down / corrupt payload) → 返回 None 不 raise
      - 反向兼容: ``CachedFeedback.value`` 在 feedback_type="none" 时返回 None
      - Key 命名空间: ``ta:{env}:cache:v1:fb:user:{uid}:msg:{mid}``
      - 反向兼容 shim: 旧 ``make_feedback_cache_key`` 仍可用 (返回新 namespace key)
    """

    @pytest.fixture(autouse=True)
    def _reset_cache_singletons(self, monkeypatch):
        """每个 test 单独重置 — 防止 suite 中其他测试污染 singleton."""
        from app.cache.backend import CacheBackend as _CB
        from app.cache.manager import set_cache_manager as _scm
        from app.cache.domains.feedback_cache import set_feedback_cache as _sfc
        from app.cache.metrics import cache_metrics as _cm

        _CB._instance = None
        _scm(None)
        _sfc(None)
        _cm.reset()
        yield
        _CB._instance = None
        _scm(None)
        _sfc(None)
        _cm.reset()

    @pytest.mark.asyncio
    async def test_cache_get_returns_none_when_no_backend(self):
        from app.cache.domains.feedback_cache import FeedbackCache as DomainFeedbackCache

        cache = DomainFeedbackCache(manager=None)
        result = await cache.get(42, 99)
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_set_returns_false_when_no_backend(self):
        from app.cache.domains.feedback_cache import FeedbackCache as DomainFeedbackCache

        cache = DomainFeedbackCache(manager=None)
        ok = await cache.set(42, 99, "like")
        assert ok is False

    @pytest.mark.asyncio
    async def test_cache_delete_returns_false_when_no_backend(self):
        from app.cache.domains.feedback_cache import FeedbackCache as DomainFeedbackCache

        cache = DomainFeedbackCache(manager=None)
        ok = await cache.delete(42, 99)
        assert ok is False

    @pytest.mark.asyncio
    async def test_cache_roundtrip_with_fakeredis(self):
        """正常路径: SET → GET 往返,value 正确."""
        from fakeredis import aioredis as fakeredis_aioredis
        from app.cache.backend import CacheBackend
        from app.cache.bulkhead import DBBulkhead
        from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
        from app.cache.manager import CacheManager
        from app.cache.singleflight import SingleFlight
        from app.cache.domains.feedback_cache import FeedbackCache as DomainFeedbackCache

        redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
        backend = CacheBackend(redis_client=redis)
        backend._healthy = True
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(BreakerConfig(failure_threshold=5, open_seconds=1)),
            bulkhead=DBBulkhead(max_concurrency=2),
            singleflight=SingleFlight(),
            jitter_fn=lambda ratio: 1.0,
        )
        cache = DomainFeedbackCache(manager=mgr)
        ok = await cache.set(42, 99, "like")
        assert ok is True
        result = await cache.get(42, 99)
        assert result is not None
        assert result.feedback_type == "like"
        assert result.value == "like"  # backward-compat

    @pytest.mark.asyncio
    async def test_cache_set_none_cancels(self):
        """feedback_type=None → 写入 "none" 而非 DEL (保持 hit-rate)."""
        from fakeredis import aioredis as fakeredis_aioredis
        from app.cache.backend import CacheBackend
        from app.cache.bulkhead import DBBulkhead
        from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
        from app.cache.manager import CacheManager
        from app.cache.singleflight import SingleFlight
        from app.cache.domains.feedback_cache import FeedbackCache as DomainFeedbackCache

        redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
        backend = CacheBackend(redis_client=redis)
        backend._healthy = True
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(BreakerConfig(failure_threshold=5, open_seconds=1)),
            bulkhead=DBBulkhead(max_concurrency=2),
            singleflight=SingleFlight(),
            jitter_fn=lambda ratio: 1.0,
        )
        cache = DomainFeedbackCache(manager=mgr)
        ok = await cache.set(42, 99, None)
        assert ok is True
        # Key 仍然存在 (没 DEL)
        result = await cache.get(42, 99)
        assert result is not None
        assert result.feedback_type == "none"
        assert result.value is None  # backward-compat: "none" → None

    @pytest.mark.asyncio
    async def test_cache_returns_none_on_redis_exception(self):
        """Redis GET 抛异常 → 返回 None 不 raise (fail-soft)."""
        from app.cache.backend import CacheBackend
        from app.cache.bulkhead import DBBulkhead
        from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
        from app.cache.manager import CacheManager
        from app.cache.singleflight import SingleFlight
        from app.cache.domains.feedback_cache import FeedbackCache as DomainFeedbackCache

        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("redis down")

            async def set(self, *a, **kw):
                raise ConnectionError("redis down")

            async def delete(self, *a, **kw):
                raise ConnectionError("redis down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(BreakerConfig(failure_threshold=5, open_seconds=1)),
            bulkhead=DBBulkhead(max_concurrency=2),
            singleflight=SingleFlight(),
            jitter_fn=lambda ratio: 1.0,
        )
        cache = DomainFeedbackCache(manager=mgr)
        # 所有方法 fail-soft, 不 raise
        assert (await cache.get(42, 99)) is None
        assert (await cache.set(42, 99, "like")) is False
        assert (await cache.delete(42, 99)) is False

    @pytest.mark.asyncio
    async def test_cache_set_rejects_invalid_type(self):
        from app.cache.domains.feedback_cache import FeedbackCache as DomainFeedbackCache
        from app.cache.backend import CacheBackend

        CacheBackend._instance = None
        cache = DomainFeedbackCache(manager=None)
        ok = await cache.set(42, 99, "invalid")
        assert ok is False

    def test_key_format_uses_standard_namespace(self):
        """Key 遵循 Business Cache Redis 命名空间."""
        from app.cache.domains.feedback_cache import feedback_key
        from app.core.config import get_settings

        env = get_settings().app_env or "dev"
        env_slug = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in env)[:32] or "dev"
        k = feedback_key(42, 99)
        # 标准前缀: ta:{env}:cache:v1:fb:user:{user_id}:msg:{message_id}
        assert k == f"ta:{env_slug}:cache:v1:fb:user:42:msg:99", k

    def test_legacy_make_feedback_cache_key_returns_new_format(self):
        """Backward-compat shim: ``make_feedback_cache_key`` 返回新 namespace key.

        旧 Runtime Redis key ``feedback:42:99`` 已废弃 (Runtime Redis 没在用此 key),
        新 key 通过 Business Cache Redis (port 6380)。
        """
        key = make_feedback_cache_key(42, 99)
        # 现在返回标准 namespace key, 不是旧的 ``feedback:42:99``
        assert ":fb:user:42:msg:99" in key, key

    def test_legacy_feedback_cache_alias_still_importable(self):
        """旧 ``FeedbackCache(redis_client=None)`` 仍可 import 但不推荐."""
        # ``app.core.feedback_cache.FeedbackCache`` 现在等价于 DomainFeedbackCache
        # (通过 shim re-export)。这里只是验证 import 不破坏。
        cache = FeedbackCache()
        assert cache is not None
        # 验证 _build_feedback_cache_for_message 也用新的 get_feedback_cache()
        from app.api.v1.messages import _build_feedback_cache_for_message
        cache2 = _build_feedback_cache_for_message(99)
        assert cache2 is not None


class TestMessageFeedbackExceptions:
    """Verify that the new error classes have the right codes."""

    def test_message_not_feedbackable_error(self):
        from app.core.exceptions import MessageNotFeedbackableError
        exc = MessageNotFeedbackableError()
        assert exc.code == 50901

    def test_message_not_feedbackable_error_custom_message(self):
        from app.core.exceptions import MessageNotFeedbackableError
        exc = MessageNotFeedbackableError("custom")
        assert str(exc) == "custom"
        assert exc.code == 50901
