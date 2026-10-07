"""Backward-compat shim — FeedbackCache moved to ``app.cache.domains.feedback_cache``.

旧文件保留是为了不破坏以下已存在代码:
  - ``from app.core.feedback_cache import FeedbackCache, make_feedback_cache_key, CachedFeedback``
  - ``app/api/v1/messages.py::_build_feedback_cache_for_message``

迁移目标 (Step 9):
  - 从 Runtime Redis (LiveEventBus, port 6379) → Business Cache Redis (port 6380)
  - 通过标准 CacheManager + CacheSpec 路径,复用 circuit breaker / singleflight /
    bulkhead / negative cache 等基础设施
  - Key namespace 从 ``feedback:<user>:<msg>`` → ``ta:{env}:cache:v1:fb:user:{user}:msg:{msg}``
  - TTL 24h 不变, negative TTL 60s
  - Domain flag ``cache_fb_enabled`` (默认 true)

新代码请直接 import ``app.cache.domains.feedback_cache``。
"""

from __future__ import annotations

# Re-export everything from the new location so old imports keep working.
from app.cache.domains.feedback_cache import (  # noqa: F401
    FEEDBACK_SPEC,
    CachedFeedback,
    FeedbackCache,
    FeedbackCacheDTO,
    feedback_key,
    get_feedback_cache,
    make_feedback_cache_key,
    set_feedback_cache,
)

__all__ = [
    "FEEDBACK_SPEC",
    "CachedFeedback",
    "FeedbackCache",
    "FeedbackCacheDTO",
    "feedback_key",
    "get_feedback_cache",
    "make_feedback_cache_key",
    "set_feedback_cache",
]