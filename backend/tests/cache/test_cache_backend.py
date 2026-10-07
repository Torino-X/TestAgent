"""Tests for ``app.cache.backend.CacheBackend``.

Coverage:
  * install / aclose lifecycle
  * is_enabled toggling (master / client presence)
  * is_installed singleton guard
  * health() PING integration
  * build_redis_client_from_settings returns None when disabled
  * build_redis_client_from_settings fails-soft when redis.asyncio missing
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.cache.backend import CacheBackend, build_redis_client_from_settings


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    CacheBackend._instance = None
    yield
    CacheBackend._instance = None


class TestSingletonLifecycle:
    async def test_get_before_install_raises(self) -> None:
        with pytest.raises(RuntimeError, match="not initialized"):
            CacheBackend.get()

    async def test_install_is_idempotent(self) -> None:
        a = await CacheBackend.install(redis_client=None)
        b = await CacheBackend.install(redis_client=None)
        assert a is b

    async def test_install_records_health(self) -> None:
        class FakeRedis:
            async def ping(self):
                return True

        await CacheBackend.install(redis_client=FakeRedis())
        assert CacheBackend.get().is_healthy is True

    async def test_install_with_failed_ping_marks_unhealthy(self) -> None:
        class FakeRedis:
            async def ping(self):
                raise ConnectionError("redis down")

        await CacheBackend.install(redis_client=FakeRedis())
        # Backend is installed (so get() works) but is_healthy=False.
        assert CacheBackend.get().is_healthy is False

    async def test_aclose_resets_singleton(self) -> None:
        class FakeRedis:
            async def aclose(self):
                pass

        await CacheBackend.install(redis_client=FakeRedis())
        assert CacheBackend.is_installed()
        await CacheBackend.aclose()
        assert not CacheBackend.is_installed()
        with pytest.raises(RuntimeError):
            CacheBackend.get()


class TestIsEnabled:
    async def test_disabled_when_no_client(self) -> None:
        await CacheBackend.install(redis_client=None)
        assert CacheBackend.get().is_enabled is False

    async def test_enabled_when_client_attached(self) -> None:
        class FakeRedis:
            async def ping(self):
                return True

        await CacheBackend.install(redis_client=FakeRedis())
        assert CacheBackend.get().is_enabled is True


class TestHealthProbe:
    async def test_health_returns_true_on_ping(self) -> None:
        class FakeRedis:
            async def ping(self):
                return True

        await CacheBackend.install(redis_client=FakeRedis())
        ok = await CacheBackend.get().health()
        assert ok is True

    async def test_health_returns_false_on_ping_fail(self) -> None:
        class FakeRedis:
            async def ping(self):
                raise ConnectionError("boom")

        await CacheBackend.install(redis_client=FakeRedis())
        ok = await CacheBackend.get().health()
        assert ok is False

    async def test_health_returns_false_when_no_client(self) -> None:
        await CacheBackend.install(redis_client=None)
        ok = await CacheBackend.get().health()
        assert ok is False


class TestBuildRedisClient:
    def test_returns_none_when_master_disabled(self) -> None:
        with patch("app.cache.backend.get_settings") as gs:
            gs.return_value.cache_redis_enabled = False
            gs.return_value.cache_redis_url = "redis://localhost:6380/0"
            assert build_redis_client_from_settings() is None

    def test_returns_none_when_url_empty(self) -> None:
        with patch("app.cache.backend.get_settings") as gs:
            gs.return_value.cache_redis_enabled = True
            gs.return_value.cache_redis_url = ""
            assert build_redis_client_from_settings() is None

    def test_returns_client_when_enabled_and_url_set(self) -> None:
        with patch("app.cache.backend.get_settings") as gs:
            gs.return_value.cache_redis_enabled = True
            gs.return_value.cache_redis_url = "redis://localhost:6380/0"
            gs.return_value.cache_redis_max_connections = 100
            gs.return_value.cache_redis_connect_timeout_ms = 200
            gs.return_value.cache_redis_socket_timeout_ms = 100
            gs.return_value.cache_redis_health_check_interval_seconds = 30
            client = build_redis_client_from_settings()
            assert client is not None

    def test_returns_none_when_redis_asyncio_missing(self) -> None:
        """When redis.asyncio import fails, the helper must return None."""
        with patch("app.cache.backend.get_settings") as gs:
            gs.return_value.cache_redis_enabled = True
            gs.return_value.cache_redis_url = "redis://localhost:6380/0"
            gs.return_value.cache_redis_max_connections = 100
            gs.return_value.cache_redis_connect_timeout_ms = 200
            gs.return_value.cache_redis_socket_timeout_ms = 100
            gs.return_value.cache_redis_health_check_interval_seconds = 30
            # Pretend `import redis.asyncio` raises ImportError.
            import sys

            saved = sys.modules.get("redis.asyncio")
            sys.modules["redis.asyncio"] = None  # forces ImportError on next import
            try:
                client = build_redis_client_from_settings()
                assert client is None
            finally:
                if saved is not None:
                    sys.modules["redis.asyncio"] = saved
                else:
                    sys.modules.pop("redis.asyncio", None)