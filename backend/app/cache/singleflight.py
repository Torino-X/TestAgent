"""SingleFlight — 进程内并发请求合并（设计文档 §18 + 提示词 §11）。

用途：同 worker 内 100 个并发请求同一 key 时，只有一个 loader 真去
DB / Redis 拉数据；其余 99 个 await 同一个 Future。

不是业务缓存；只合并并发请求，不存结果。

要求:
  - 同 key 同时只有一个 loader
  - leader exception 正确传播给 waiter
  - finally 必须清理
  - 不产生 Future 泄漏
  - shutdown 可清理
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SingleFlight:
    """Process-local in-flight request merger."""

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._lock = asyncio.Lock()

    async def do(
        self,
        key: str,
        loader: Callable[[], Awaitable[T]],
    ) -> T:
        """Run ``loader()`` at most once per key while it is in-flight.

        Multiple concurrent callers for the same key will all observe the
        same return value (or the same exception).  When the loader
        finishes (success or error), the entry is removed and subsequent
        callers will run a fresh loader.
        """
        loop = asyncio.get_running_loop()
        # Fast path: leader already exists → join it.
        fut = self._inflight.get(key)
        if fut is not None and not fut.done():
            return await asyncio.shield(fut)

        # Slow path: become the leader under lock.
        async with self._lock:
            fut = self._inflight.get(key)
            if fut is not None and not fut.done():
                return await asyncio.shield(fut)
            fut = loop.create_future()
            self._inflight[key] = fut

        try:
            result = await loader()
            if not fut.done():
                fut.set_result(result)
            return result
        except BaseException as exc:  # noqa: BLE001 — propagate to all waiters
            if not fut.done():
                fut.set_exception(exc)
                # The leader raises ``exc`` directly. With no joined waiters,
                # nobody awaits this coordination Future and asyncio would
                # otherwise emit "Future exception was never retrieved".
                # Retrieving it here does not alter what joined waiters await.
                fut.exception()
            raise
        finally:
            # Best-effort cleanup; if another caller raced and re-created
            # the entry, leave their future alone.
            cur = self._inflight.get(key)
            if cur is fut:
                self._inflight.pop(key, None)

    def inflight_count(self) -> int:
        return len(self._inflight)

    def keys(self) -> list[str]:
        return list(self._inflight.keys())


__all__ = ["SingleFlight"]
