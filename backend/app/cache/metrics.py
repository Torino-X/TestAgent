"""Cache metrics — 最小指标适配器（设计文档 §26）。

统计维度:
  - domain（auth / cfg / conv / task / lib / ctx / sem）
  - operation（get / set / delete / fill / invalidate）
  - result（hit / miss / negative_hit / error / bypass）
  - latency_ms（histogram 简化版 — 仅记录最后一次 + p95 估算窗口）

不依赖任何外部 metrics 框架；后续可挂到 OpenTelemetry / Prometheus。
日志禁止输出 payload / API Key / JWT / Password / 完整 Redis URL 密码。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

logger = logging.getLogger(__name__)

MAX_LATENCY_WINDOW = 100  # per (domain, operation)


@dataclass
class _BucketKey:
    domain: str
    operation: str
    result: str

    def __hash__(self) -> int:
        return hash((self.domain, self.operation, self.result))


@dataclass
class _Bucket:
    count: int = 0
    latencies: Deque[float] = field(default_factory=lambda: deque(maxlen=MAX_LATENCY_WINDOW))

    def observe(self, latency_ms: float) -> None:
        self.count += 1
        self.latencies.append(latency_ms)


class CacheMetrics:
    """Process-wide metrics aggregator for cache operations."""

    def __init__(self) -> None:
        self._buckets: dict[_BucketKey, _Bucket] = {}

    def record(
        self,
        *,
        domain: str,
        operation: str,
        result: str,
        latency_ms: float | None = None,
    ) -> None:
        key = _BucketKey(domain, operation, result)
        bucket = self._buckets.setdefault(key, _Bucket())
        if latency_ms is None:
            latency_ms = 0.0
        bucket.observe(latency_ms)

    def snapshot(self) -> dict[tuple[str, str, str], int]:
        """Return a copy of all buckets' counts (no latency samples)."""
        return {
            (k.domain, k.operation, k.result): b.count
            for k, b in self._buckets.items()
        }

    def reset(self) -> None:
        self._buckets.clear()


# ── Convenience timing helper ──────────────────────────────────────


class Timer:
    """Tiny context manager that yields elapsed_ms on exit."""

    __slots__ = ("_t0", "elapsed_ms")

    def __init__(self) -> None:
        self._t0 = 0.0
        self.elapsed_ms = 0.0

    def __enter__(self) -> "Timer":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.elapsed_ms = (time.perf_counter() - self._t0) * 1000.0


cache_metrics = CacheMetrics()
"""Module-level singleton — Domain Cache modules share one metrics instance."""


__all__ = ["CacheMetrics", "Timer", "cache_metrics"]