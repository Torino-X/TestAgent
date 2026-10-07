"""CacheBackend — Business Cache Redis 共享 client 单例 + lifespan 启停。

职责:
  - lifespan 阶段根据 ``settings.cache_redis_url`` 构造一个 ``redis.asyncio``
    连接池（所有 worker / 所有 Domain Cache 共享）。
  - 健康检查：定期 PING；失败 → 标记 degraded；不阻塞业务启动。
  - 优雅关闭：lifespan 退出时 aclose。

设计参考 docs/prompt/TestAgent_Redis缓存系统详细技术设计文档.md §29。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class CacheBackend:
    """Business Cache Redis 客户端单例。

    ``CacheBackend.get()`` 在 lifespan 启动期被实例化一次；之后所有
    Domain Cache 通过 ``CacheBackend.get().client`` 拿到同一个连接池。
    """

    _instance: "CacheBackend | None" = None

    def __init__(self, *, redis_client: Any | None = None) -> None:
        self._redis = redis_client
        self._healthy: bool = redis_client is not None

    # ── Accessors ───────────────────────────────────────────────────

    @classmethod
    def get(cls) -> "CacheBackend":
        """Return the lifespan-installed singleton (raises if not started)."""
        if cls._instance is None:
            raise RuntimeError(
                "CacheBackend not initialized — main.py lifespan must call "
                "CacheBackend.install(...) before any Domain Cache is used"
            )
        return cls._instance

    @classmethod
    def is_installed(cls) -> bool:
        return cls._instance is not None

    @property
    def is_enabled(self) -> bool:
        """True when master toggle is on AND a redis client is attached.

        Note: we deliberately do NOT check ``cache_redis_url`` here.  The
        URL check happens in ``build_redis_client_from_settings()`` — if
        that helper returned ``None``, ``redis_client`` is also None and
        is_enabled is False for the right reason.  Splitting the URL
        check this way lets tests inject a fakeredis client directly
        (URL stays empty) and still exercise the cache.
        """
        settings = get_settings()
        return bool(settings.cache_redis_enabled and self._redis is not None)

    @property
    def is_healthy(self) -> bool:
        return self._healthy

    @property
    def client(self) -> Any:
        """Return the underlying redis.asyncio client (raises if disabled)."""
        if self._redis is None:
            raise RuntimeError(
                "CacheBackend is disabled (cache_redis_url empty or master "
                "toggle off); Domain Cache must call is_enabled first"
            )
        return self._redis

    # ── Health probe ────────────────────────────────────────────────

    async def health(self) -> bool:
        """Synchronous PING.  Updates ``is_healthy`` flag.

        Returns ``True`` when PING succeeded; ``False`` otherwise.  Never
        raises.  The lifespan can schedule this on a background loop
        (设计文档 §30 — ``CACHE_REDIS_HEALTH_CHECK_INTERVAL_SECONDS``).
        """
        if self._redis is None:
            return False
        try:
            pong = await self._redis.ping()
            self._healthy = bool(pong)
        except Exception as exc:  # noqa: BLE001 — fail-soft
            self._healthy = False
            logger.debug("CacheBackend.health: PING failed: %s", exc)
        return self._healthy

    # ── Lifecycle ───────────────────────────────────────────────────

    @classmethod
    async def install(
        cls,
        *,
        redis_client: Any | None = None,
    ) -> "CacheBackend":
        """Lifespan startup hook.  Idempotent.

        ``redis_client`` is the redis.asyncio client (or None to disable).
        Passing ``None`` while master toggle is on is a no-op (degraded
        mode) — all Domain Cache bypass until a real client is supplied.

        For production lifespan, call ``build_redis_client_from_settings()``
        first to get the client, then ``install(redis_client=client)``.
        """
        if cls._instance is not None:
            logger.debug("CacheBackend.install: already initialized; skipping")
            return cls._instance
        instance = cls(redis_client=redis_client)
        # Try a PING if we have a client; failure keeps the backend in
        # disabled state but does not raise (graceful degradation per
        # 设计文档 §21.1).
        if redis_client is not None:
            try:
                pong = await redis_client.ping()
                instance._healthy = bool(pong)
            except Exception as exc:  # noqa: BLE001
                instance._healthy = False
                logger.warning(
                    "CacheBackend.install: initial PING failed; degraded | err=%s",
                    exc,
                )
        cls._instance = instance
        logger.info(
            "CacheBackend.install: enabled=%s healthy=%s",
            instance.is_enabled,
            instance._healthy,
        )
        return instance

    @classmethod
    async def aclose(cls) -> None:
        """Lifespan shutdown hook."""
        inst = cls._instance
        cls._instance = None
        if inst is None or inst._redis is None:
            return
        try:
            if hasattr(inst._redis, "aclose"):
                await inst._redis.aclose()
            elif hasattr(inst._redis, "close"):
                close_coro = inst._redis.close()
                if hasattr(close_coro, "__await__"):
                    await close_coro
        except Exception as exc:  # noqa: BLE001 — fail-soft shutdown
            logger.warning("CacheBackend.aclose failed: %s", exc)


# ── Module-level helper: build real redis.asyncio client ──────────


def build_redis_client_from_settings() -> Any:
    """Construct a real ``redis.asyncio`` client from ``cache_*`` settings.

    Returns ``None`` when the master toggle is off or URL is empty — the
    lifespan then installs a disabled backend (all Domain Cache bypass).

    Settings honoured:
      - cache_redis_url
      - cache_redis_max_connections
      - cache_redis_connect_timeout_ms
      - cache_redis_socket_timeout_ms

    Kept separate from ``install`` so unit tests can ``install``
    with their own (fakeredis) client while production lifespan goes
    through this helper.
    """
    settings = get_settings()
    if not settings.cache_redis_enabled or not settings.cache_redis_url:
        return None
    try:
        import redis.asyncio as redis_async  # type: ignore
    except ImportError:
        logger.error(
            "build_redis_client_from_settings: redis.asyncio unavailable; "
            "install 'redis>=5.0.0,<6.0.0'"
        )
        return None
    # Per 设计文档 §21.4 + §30: connect timeout / socket timeout are
    # fail-fast — must not block the request loop.
    socket_timeout = settings.cache_redis_socket_timeout_ms / 1000.0
    connect_timeout = settings.cache_redis_connect_timeout_ms / 1000.0
    return redis_async.from_url(
        settings.cache_redis_url,
        max_connections=int(settings.cache_redis_max_connections),
        socket_connect_timeout=connect_timeout,
        socket_timeout=socket_timeout,
        retry_on_timeout=False,
        health_check_interval=int(
            getattr(settings, "cache_redis_health_check_interval_seconds", 30)
        ),
        decode_responses=False,
    )


__all__ = ["CacheBackend", "build_redis_client_from_settings"]