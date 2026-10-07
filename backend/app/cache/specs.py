"""CacheSpec — 缓存规格的不可变定义。

每个 Domain Cache 在声明时构造一个 ``CacheSpec`` 实例，集中表达:

  * ``domain`` — 命名空间（auth / cfg / conv / task / lib / ctx / sem）
  * ``ttl_seconds`` — 正缓存基础 TTL
  * ``negative_ttl_seconds`` — 负缓存 TTL（穿透防护）
  * ``jitter_ratio`` — TTL ±抖动比例（防雪崩）
  * ``enable_singleflight`` — 是否启用进程内 SingleFlight
  * ``enable_distributed_fill_lock`` — 是否启用 Redis cache-fill 锁
  * ``max_value_bytes`` — 单 value 字节上限（防 MEDIUMTEXT 污染）

详细设计参考 docs/prompt/TestAgent_Redis缓存系统详细技术设计文档.md §31。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CacheSpec:
    """缓存规格的不可变定义（frozen=True 保证 hashable + 线程安全）。"""

    domain: str
    ttl_seconds: int
    negative_ttl_seconds: int | None = None
    jitter_ratio: float = 0.10
    enable_singleflight: bool = True
    enable_distributed_fill_lock: bool = False
    max_value_bytes: int = 262144


__all__ = ["CacheSpec"]