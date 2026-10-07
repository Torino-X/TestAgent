"""Circuit Breaker — Redis 操作轻量熔断（设计文档 §21.2）。

阈值/时间来自 settings:
  - CACHE_BREAKER_FAILURE_THRESHOLD（默认 5）
  - CACHE_BREAKER_OPEN_SECONDS（默认 5）

行为:
  - closed：正常执行 Redis 操作
  - open：直接 fast-fail，不再等待 socket timeout；fallback DB
  - half-open：放少量探测请求；成功 → closed，失败 → open 再等

本会话仅实现状态机；CacheManager 在 Step 3 包装实际 Redis 操作调用。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when an operation is short-circuited by an open breaker."""


@dataclass
class BreakerConfig:
    failure_threshold: int
    open_seconds: float
    half_open_max_probes: int = 1


def default_breaker_config() -> BreakerConfig:
    s = get_settings()
    return BreakerConfig(
        failure_threshold=int(getattr(s, "cache_breaker_failure_threshold", 5)),
        open_seconds=float(getattr(s, "cache_breaker_open_seconds", 5)),
    )


class CircuitBreaker:
    """Async-friendly circuit breaker.

    Not thread-safe; intended for asyncio (one event loop per breaker).
    """

    def __init__(self, config: BreakerConfig | None = None) -> None:
        self._cfg = config or default_breaker_config()
        self._state: BreakerState = BreakerState.CLOSED
        self._failure_count: int = 0
        self._opened_at: float = 0.0
        self._half_open_in_flight: int = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> BreakerState:
        return self._state

    async def _enter(self) -> None:
        """Check state transitions; raise if open."""
        if self._state is BreakerState.CLOSED:
            return
        if self._state is BreakerState.OPEN:
            if (time.monotonic() - self._opened_at) >= self._cfg.open_seconds:
                async with self._lock:
                    if self._state is BreakerState.OPEN:
                        self._state = BreakerState.HALF_OPEN
                        self._half_open_in_flight = 0
                        logger.info(
                            "CircuitBreaker: OPEN → HALF_OPEN (probe=%d)",
                            self._cfg.half_open_max_probes,
                        )
            else:
                raise CircuitOpenError("circuit breaker is OPEN")
        # HALF_OPEN: only allow up to N concurrent probes
        if self._state is BreakerState.HALF_OPEN:
            async with self._lock:
                if self._half_open_in_flight >= self._cfg.half_open_max_probes:
                    raise CircuitOpenError("circuit breaker is HALF_OPEN (probe full)")
                self._half_open_in_flight += 1

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state is BreakerState.HALF_OPEN:
                self._state = BreakerState.CLOSED
                self._half_open_in_flight = 0
                logger.info("CircuitBreaker: HALF_OPEN → CLOSED (probe ok)")
            self._failure_count = 0

    async def _on_failure(self) -> None:
        async with self._lock:
            if self._state is BreakerState.HALF_OPEN:
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
                self._half_open_in_flight = 0
                logger.warning(
                    "CircuitBreaker: HALF_OPEN → OPEN (probe failed); open %.1fs",
                    self._cfg.open_seconds,
                )
                return
            self._failure_count += 1
            if self._failure_count >= self._cfg.failure_threshold:
                if self._state is BreakerState.CLOSED:
                    self._state = BreakerState.OPEN
                    self._opened_at = time.monotonic()
                    logger.warning(
                        "CircuitBreaker: CLOSED → OPEN (failures=%d); open %.1fs",
                        self._failure_count,
                        self._cfg.open_seconds,
                    )

    async def call(self, coro_factory):  # type: ignore[no-untyped-def]
        """Run ``coro_factory()`` under the breaker; re-raise on failure."""
        await self._enter()
        try:
            result = await coro_factory()
        except BaseException:
            await self._on_failure()
            raise
        else:
            await self._on_success()
            return result


__all__ = ["BreakerState", "BreakerConfig", "CircuitBreaker", "CircuitOpenError"]