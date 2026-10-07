"""Distributed cache-fill lock — Redis SET NX PX + token compare (设计文档 §18.2)。

仅热点 spec 启用（auth / model_config / knowledge_config / conversation_list /
system_config）。不是业务分布式事务锁——禁止升级为 Redlock。

释放用 WATCH + GET + MULTI/DEL/EXEC，避免 owner mismatch 时误删别人锁。
WATCH/MULTI/EXEC 在 Redis 与 fakeredis 中均原生支持（不需要 Lua/lupa）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Any

logger = logging.getLogger(__name__)


class CacheFillLock:
    """Per-key cache-fill lock backed by Redis ``SET NX PX``.

    Usage::

        lock = CacheFillLock(redis_client)
        async with await lock.acquire("cfg:model:user:1") as owner:
            if owner is None:
                # someone else is filling; sleep + retry cache
                ...
            else:
                # we own the fill; do DB read + cache set
                ...
    """

    def __init__(
        self,
        redis_client: Any,
        *,
        ttl_ms: int = 2000,
        key_prefix: str = "ta:lock:",
    ) -> None:
        self._redis = redis_client
        self._ttl_ms = int(ttl_ms)
        self._prefix = key_prefix

    def _key(self, k: str) -> str:
        return f"{self._prefix}{k}"

    async def acquire(self, k: str) -> "_LockOwner | None":
        """Try to acquire the lock.  Returns ``None`` if held by another."""
        token = secrets.token_hex(8) + "-" + str(os.getpid())
        try:
            ok = await self._redis.set(
                self._key(k), token, nx=True, px=self._ttl_ms
            )
        except Exception as exc:  # noqa: BLE001 — fail-soft
            logger.warning("CacheFillLock.acquire(%s): Redis SET failed: %s", k, exc)
            return None
        if not ok:
            return None
        return _LockOwner(self, k, token)

    async def release(self, k: str, token: str) -> bool:
        """Release only when the held token matches (owner-safe).

        Uses WATCH → GET → MULTI/DEL/EXEC.  If the GET shows a
        different token (someone else holds the lock now, or the TTL
        already expired and someone re-acquired) we UNWATCH and return
        False without deleting.
        """
        key = self._key(k)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                while True:
                    try:
                        await pipe.watch(key)
                        current = await pipe.get(key)
                        if current is None:
                            await pipe.unwatch()
                            return False
                        if isinstance(current, (bytes, bytearray)):
                            current = current.decode("utf-8", errors="replace")
                        if current != token:
                            await pipe.unwatch()
                            return False
                        pipe.multi()
                        pipe.delete(key)
                        result = await pipe.execute()
                        # execute returns [delete_result]; 1 = deleted
                        return bool(result and result[0])
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # WATCH conflict (concurrent change) → retry once.
                        # For our use case (TTL-bound lock) one retry is enough.
                        await pipe.reset()
                        return False
        except Exception as exc:  # noqa: BLE001 — fail-soft
            logger.warning("CacheFillLock.release(%s): WATCH/DEL failed: %s", k, exc)
            return False


class _LockOwner:
    """Async-context-manager wrapper returned by ``CacheFillLock.acquire``."""

    __slots__ = ("_lock", "_key", "_token", "_released")

    def __init__(self, lock: CacheFillLock, key: str, token: str) -> None:
        self._lock = lock
        self._key = key
        self._token = token
        self._released = False

    @property
    def token(self) -> str:
        return self._token

    async def __aenter__(self) -> "_LockOwner":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if not self._released:
            self._released = True
            await self._lock.release(self._key, self._token)


__all__ = ["CacheFillLock"]