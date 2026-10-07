"""DB Fallback Bulkhead — cache loader 走 DB 时的并发闸门（设计文档 §21.3）。

Cache miss / Redis down 导致的 cache loader 使用独立并发限制，每 worker
默认 5 个并发，防止 Redis 故障时打穿 MySQL 连接池。

只限制"缓存回源型读取"；不限制正常事务写 / Agent write / Outbox /
confirmation 路径。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from app.core.config import get_settings
from app.core.exceptions import AppError

logger = logging.getLogger(__name__)


class BulkheadTimeoutError(AppError):
    """Cache fallback DB bulkhead 排队超时。

    AppError code: 50403（504xx = 限流/资源耗尽；具体语义按设计文档 §21.3）。
    """

    def __init__(self, message: str = "DB fallback bulkhead timeout") -> None:
        super().__init__(50403, message)


class DBBulkhead:
    """Per-worker semaphore limiting concurrent cache-loader DB reads."""

    def __init__(self, max_concurrency: int | None = None) -> None:
        s = get_settings()
        cap = max_concurrency
        if cap is None:
            cap = int(getattr(s, "cache_db_fallback_max_concurrency", 5))
        self._max = max(1, int(cap))
        self._sem = asyncio.Semaphore(self._max)
        self._in_use: int = 0

    @property
    def max_concurrency(self) -> int:
        return self._max

    @property
    def in_use(self) -> int:
        return self._in_use

    @asynccontextmanager
    async def acquire(self, *, timeout: float | None = None):
        """Acquire a slot; raise BulkheadTimeoutError on queue timeout."""
        try:
            if timeout is None:
                await self._sem.acquire()
            else:
                await asyncio.wait_for(self._sem.acquire(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            logger.warning(
                "DBBulkhead.acquire: timeout (in_use=%d max=%d)",
                self._in_use, self._max,
            )
            raise BulkheadTimeoutError() from exc
        self._in_use += 1
        try:
            yield
        finally:
            self._in_use -= 1
            self._sem.release()


__all__ = ["DBBulkhead", "BulkheadTimeoutError"]