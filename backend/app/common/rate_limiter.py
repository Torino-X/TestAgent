"""Async-safe token bucket rate limiter — adapted from legacy rate_limiter.py.

Threading-based token bucket replaced with an asyncio-aware implementation.
All capabilities preserved: configurable requests-per-minute, max burst,
infinite-rate bypass (rate=0), and wait-before-acquire.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class RateLimiter:
    """Async-safe token-bucket rate limiter for API calls.

    Usage::

        limiter = RateLimiter(max_requests_per_minute=15, max_burst=3)
        await limiter.acquire()  # blocks until a token is available
    """

    def __init__(self, max_requests_per_minute: int = 0, max_burst: int = 3) -> None:
        self._max_burst = max(max_burst, 1)
        if max_requests_per_minute > 0:
            self._refill_rate = max_requests_per_minute / 60.0  # tokens / second
        else:
            self._refill_rate = float("inf")  # unlimited
        self._tokens = float(self._max_burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire one token, waiting if necessary."""
        if self._refill_rate == float("inf"):
            return

        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    float(self._max_burst),
                    self._tokens + elapsed * self._refill_rate,
                )
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                wait = (1.0 - self._tokens) / self._refill_rate

            logger.debug(
                "令牌桶限流等待 | 需等待: %.1fs | 当前令牌: %.2f | 恢复速率: %.2f/s",
                wait, max(0.0, self._tokens), self._refill_rate,
            )
            await asyncio.sleep(min(wait, 1.0))


# Legacy alias for callers expecting the old name
TokenBucketRateLimiter = RateLimiter


# 模块定位:asyncio 安全的令牌桶限流器(legacy threading → asyncio 重写)
#
# 能力:
#   - 可配置 req/min 与最大突发(infinite rate=0 旁路)
#   - wait-before-acquire 语义(`await limiter.acquire()`)
#   - 100% async-safe(不阻塞 event loop)
#
# 链路:
#   任何 LLM 上游调用前的 guard:
#   provider_factory.create(...) → RateLimiter(token_budget)
#     → 每个 LLM request → `await limiter.acquire()` → 慢时减慢
#
# 关键约束:
#   - 不要把 RateLimiter 实例挪到 thread 上下文(跨线程计数会乱);
#   - 多 worker 部署时,**每个进程独立计数**(不需要 Redis 同步,
#     真实 token usage 由 LLM provider 自己限);
#   - 测试允许 monkeypatch clock。
