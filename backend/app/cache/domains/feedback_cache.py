"""Feedback Cache (Step 9 — 迁移到 Business Cache Redis).

设计文档 §11.5 + 提示词 §28:
  - **迁移目标**: 原来 ``app.core.feedback_cache.FeedbackCache`` 用
    Runtime Redis (LiveEventBus 端口 6379), 现在统一到 Business Cache
    Redis (端口 6380, allkeys-lfu), 走标准 CacheManager 路径。
  - **Key**: ``ta:{env}:cache:v1:fb:user:{user_internal_id}:msg:{message_internal_id}``
  - **TTL**: 24h (同原值) — feedback 是 low-write / high-read, 24h 与 LLMConfigCache 对齐
  - **Negative TTL**: 60s — 防穿透
  - **Domain flag**: ``cache_fb_enabled`` (默认 true)
  - **Fail-soft**: 同其他 Domain Cache — Redis down / disabled → get 返回 None,
    set 返回 False, 业务继续走 MySQL。
  - **多 worker 一致**: 通过 Business Cache Redis 实例 (port 6380) 共享。

存储格式: ``{"v": 1, "negative": false, "created_at": ..., "payload": {"feedback_type": "like"}}``
由 ``CacheManager.set`` 编码为 JSON envelope (设计文档 §7);``feedback_type`` ∈
{"like", "dislike", "none"}, "none" 表示已清除 (保持 hit-rate 稳定, 避免 toggle 抖动)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.cache.key_builder import build_key
from app.cache.manager import CacheManager, get_cache_manager
from app.cache.specs import CacheSpec

logger = logging.getLogger(__name__)


# ── DTO ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FeedbackCacheDTO:
    """Cache 内的 feedback 值投影 — 只有 feedback_type 一字段.

    与原 ``CachedFeedback`` 兼容 (``value`` 属性)。
    """

    feedback_type: str  # "like" | "dislike" | "none"

    def to_dict(self) -> dict[str, Any]:
        return {"feedback_type": self.feedback_type}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeedbackCacheDTO":
        ft = data.get("feedback_type", "none")
        if ft not in ("like", "dislike", "none"):
            ft = "none"
        return cls(feedback_type=ft)

    @property
    def value(self) -> str | None:
        """向后兼容 — ``CachedFeedback.value`` 返回 None 当为 "none"."""
        return self.feedback_type if self.feedback_type in ("like", "dislike") else None


# ── Spec ────────────────────────────────────────────────────────────


# Per 设计文档 §11.5: 24h TTL, 60s negative, 走 fill_lock (高读)。
FEEDBACK_SPEC = CacheSpec(
    domain="fb",
    ttl_seconds=24 * 3600,
    negative_ttl_seconds=60,
    jitter_ratio=0.10,
    enable_singleflight=True,
    enable_distributed_fill_lock=False,  # key 高度分散, 单 worker 已够
    max_value_bytes=512,  # DTO 极小
)


# ── Key helpers ────────────────────────────────────────────────────


def feedback_key(user_internal_id: int, message_internal_id: int) -> str:
    """Build the canonical Redis key for a (user, message) feedback entry.

    与原 ``make_feedback_cache_key`` 兼容格式 (但 namespace 改到标准前缀)。
    """
    return build_key("fb", "user", user_internal_id, "msg", message_internal_id)


# ── Cache service ─────────────────────────────────────────────────


class FeedbackCache:
    """Per-(user, message) feedback value cache via Business Cache Redis.

    行为契约 (与原 ``app.core.feedback_cache.FeedbackCache`` 一致):
      - ``redis_client is None`` / backend disabled → 所有方法返回 None / False
      - Redis GET/SET/DEL 抛异常 → get 返回 None, set/delete 返回 False (fail-soft)
      - "none" 表示清除 (不 DEL key, 保持 hit-rate 稳定 — 设计文档 §11.5 第 4 条)
    """

    def __init__(
        self,
        *,
        manager: CacheManager | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._mgr = manager if manager is not None else get_cache_manager()
        # 允许测试覆盖 TTL (默认 24h)
        self._ttl_override = ttl_seconds

    @property
    def spec(self) -> CacheSpec:
        if self._ttl_override is None:
            return FEEDBACK_SPEC
        # 测试场景: 允许覆盖 TTL 但保留其余 invariant
        return CacheSpec(
            domain=FEEDBACK_SPEC.domain,
            ttl_seconds=self._ttl_override,
            negative_ttl_seconds=FEEDBACK_SPEC.negative_ttl_seconds,
            jitter_ratio=FEEDBACK_SPEC.jitter_ratio,
            enable_singleflight=FEEDBACK_SPEC.enable_singleflight,
            enable_distributed_fill_lock=FEEDBACK_SPEC.enable_distributed_fill_lock,
            max_value_bytes=FEEDBACK_SPEC.max_value_bytes,
        )

    def _key(self, user_internal_id: int, message_internal_id: int) -> str:
        return feedback_key(user_internal_id, message_internal_id)

    # ── Read path ───────────────────────────────────────────────────

    async def get(
        self, user_internal_id: int, message_internal_id: int
    ) -> FeedbackCacheDTO | None:
        """Returns cached feedback or ``None`` if absent / unavailable.

        ``None`` covers two cases (deliberately indistinguishable):
          1. cache miss (key not in Redis yet)
          2. cache unavailable (Redis down / domain disabled)
        Callers should fall back to MySQL on ``None`` and call :meth:`set`
        to repopulate after a successful read.
        """
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec.domain):
            return None
        key = self._key(user_internal_id, message_internal_id)
        cached = await self._mgr.get(self.spec, key)
        from app.cache.manager import _CacheMiss as _CM

        if isinstance(cached, _CM):
            return None  # miss / negative / corrupt / bypass → fallback DB
        if isinstance(cached, dict):
            return FeedbackCacheDTO.from_dict(cached)
        # Defensive: unexpected type
        return None

    # ── Write-through (after DB commit) ────────────────────────────

    async def set(
        self,
        user_internal_id: int,
        message_internal_id: int,
        feedback_type: str | None,
    ) -> bool:
        """Writes ``"like"/"dislike"/"none"`` to Redis. Returns True on success.

        Args:
            feedback_type: ``"like"``, ``"dislike"``, or ``None`` (cleared →
                stored as ``"none"``).  Anything else → False (validation).

        Failure modes that return False (callers keep going):
          - Backend disabled or domain flag off
          - payload > ``max_value_bytes``
          - Redis SET error / breaker open
        """
        if feedback_type not in ("like", "dislike", None):
            return False
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec.domain):
            return False
        ft = feedback_type or "none"
        dto = FeedbackCacheDTO(feedback_type=ft)
        key = self._key(user_internal_id, message_internal_id)
        return await self._mgr.set(self.spec, key, dto.to_dict())

    async def delete(
        self, user_internal_id: int, message_internal_id: int
    ) -> bool:
        """Drop the cached entry (admin reset).  Returns True on success.

        Per 设计文档 §11.5: 生产代码**不要**用 delete 清空 feedback —
        应该用 ``set(..., "none")`` 保持 key 存在以稳定 hit-rate.
        本方法留给 admin reset 路径。
        """
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec.domain):
            return False
        key = self._key(user_internal_id, message_internal_id)
        return await self._mgr.delete(self.spec, key)


# ── Module-level singleton ─────────────────────────────────────────


_cache: FeedbackCache | None = None


def get_feedback_cache() -> FeedbackCache:
    """Return the lifespan-installed singleton (lazy default).

    Production code calls this; tests can override via :func:`set_feedback_cache`.
    """
    global _cache
    if _cache is None:
        _cache = FeedbackCache()
    return _cache


def set_feedback_cache(c: FeedbackCache | None) -> None:
    """Install/reset the singleton (mainly for tests)."""
    global _cache
    _cache = c


# ── Backward-compat shims (old API in app.core.feedback_cache) ────


# 保持旧 ``from app.core.feedback_cache import FeedbackCache`` 能继续工作
# (旧文件保留作 deprecation shim,见 app/core/feedback_cache.py)。
# 旧 ``CachedFeedback`` 改为 DTO 的别名:
CachedFeedback = FeedbackCacheDTO
"""Deprecated: 旧 ``CachedFeedback.value`` → ``FeedbackCacheDTO.value`` (alias)."""


def make_feedback_cache_key(
    user_internal_id: int,
    message_internal_id: int,
) -> str:
    """Deprecated: 保留旧 API 但返回新 namespace key.

    旧实现返回 ``"feedback:<user_id>:<message_id>"`` (Runtime Redis prefix),
    新实现返回 ``ta:{env}:cache:v1:fb:user:{user_id}:msg:{message_id}``。
    """
    return feedback_key(user_internal_id, message_internal_id)


__all__ = [
    "FEEDBACK_SPEC",
    "FeedbackCache",
    "FeedbackCacheDTO",
    "CachedFeedback",
    "feedback_key",
    "get_feedback_cache",
    "make_feedback_cache_key",
    "set_feedback_cache",
]