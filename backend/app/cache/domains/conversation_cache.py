"""Conversation Cache (设计文档 §12 + 提示词 §7, §21, §27).

消除每次 GET /api/conversations 都跑 ``list_by_user`` + ``count_by_user`` +
``count_group_by_conversation`` × 2 的高并发压力. Phase 0 已经把 SQL
从 ``2 + 2N`` 压到 ``≤4``; Phase 1 再把大多数请求变成 0 SQL.

设计要点 (设计文档 §12.2-§12.4):

  List Key:   ta:{env}:cache:v1:conv:list:user:{uid}:g:{generation}:q:{query_hash}
  Detail Key: ta:{env}:cache:v1:conv:detail:user:{uid}:conv:{public_id}
  Gen Key:    ta:{env}:cache:v1:gen:conv:user:{uid}

  TTL:        list 60s / detail 60s / negative 30s
  Gen token:  持久 (无 TTL); bump 时随机重新生成

失效策略 (设计文档 §12.3 + 提示词 §21):

  旧 list Key 不扫描删除, 改 bump generation token. Reader 用新 token
  拼 key 后命中失败 → 重新 DB load + 写新缓存. 60s 内旧缓存自然过期.

  bump_generation 触发点 (覆盖所有影响 list 内容的写穿):
    - conversation_service.create          (新会话可见)
    - conversation_service.update_title    (title / updated_at 变)
    - conversation_service.delete          (软删, list 排除)
    - message_service.send_message         (message_count + updated_at 变)
    - file_service.upload                  (file_count 变)
    - file_service.delete                  (file_count 变)

Detail Key 是单条精确 Key, 不需要 generation — bump generation 已经让
旧 list 不可达; 写 detail 失效时直接 delete. 60s 内自然过期.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.cache.distributed_lock import CacheFillLock
from app.cache.key_builder import build_key, hash_filter
from app.cache.manager import CacheManager, get_cache_manager
from app.cache.metrics import cache_metrics
from app.cache.specs import CacheSpec

logger = logging.getLogger(__name__)


# ── Spec ────────────────────────────────────────────────────────────


# Per 设计文档 §12.4: TTL 60s (±10% jitter), negative 30s
CONVERSATION_LIST_SPEC = CacheSpec(
    domain="conv",
    ttl_seconds=60,
    negative_ttl_seconds=30,
    jitter_ratio=0.10,
    enable_singleflight=True,
    enable_distributed_fill_lock=True,
)

CONVERSATION_DETAIL_SPEC = CacheSpec(
    domain="conv",
    ttl_seconds=60,
    negative_ttl_seconds=30,
    jitter_ratio=0.10,
    enable_singleflight=True,
    enable_distributed_fill_lock=False,  # detail miss 不太并发
)


# Generation Key 没有 TTL — 持久保留直到 bump
GENERATION_SPEC = CacheSpec(
    domain="conv",
    ttl_seconds=60 * 60 * 24 * 30,  # 30 天 (实际由 bump 触发轮转)
    jitter_ratio=0.0,
    enable_singleflight=False,
    enable_distributed_fill_lock=False,
)


# ── DTO ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConversationListDTO:
    """Cached list response shape.

    Mirrors ``ConversationService.list_conversations``'s tuple return:
    ``(summaries, total)``.  ``summaries`` is a list of dicts; ``total``
    is the count_by_user integer.
    """

    summaries: list[dict[str, Any]]
    total: int

    def to_dict(self) -> dict[str, Any]:
        return {"summaries": list(self.summaries), "total": self.total}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationListDTO":
        return cls(
            summaries=list(data.get("summaries", [])),
            total=int(data.get("total", 0)),
        )


@dataclass(frozen=True)
class ConversationDetailDTO:
    """Cached detail response shape (matches ``get_detail`` return)."""

    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"payload": dict(self.payload)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationDetailDTO":
        return cls(payload=dict(data.get("payload", {})))


# ── Key helpers ────────────────────────────────────────────────────


def conv_list_key(user_id: int | str, generation: str, query_hash: str = "all") -> str:
    """List cache key — generation token + query hash segment."""
    return build_key("conv", "list", "user", user_id, "g", generation, "q", query_hash)


def conv_detail_key(user_id: int | str, conv_public_id: str) -> str:
    return build_key("conv", "detail", "user", user_id, "conv", conv_public_id)


def conv_generation_key(user_id: int | str) -> str:
    """Per-user generation token Key (设计文档 §12.2)."""
    return build_key("gen", "conv", "user", user_id)


# ── Cache service ─────────────────────────────────────────────────


class ConversationCache:
    """Per-user conversation list / detail cache with generation tokens.

    Lifecycle:
      - ``get_or_load_list(user_id, loader)`` — Chat sidebar initial load.
      - ``get_or_load_detail(user_id, conv_id, loader)`` — Conversation detail.
      - ``bump_generation(user_id)`` — Rotate the per-user generation token
        so all old list cache entries become unreachable.
      - ``invalidate_detail(user_id, conv_id)`` — Single detail entry.

    ``loader()`` must return ``ConversationListDTO`` / ``ConversationDetailDTO``
    (or ``None`` for not-found / soft-deleted).
    """

    def __init__(
        self,
        manager: CacheManager | None = None,
        *,
        fill_lock: CacheFillLock | None = None,
    ) -> None:
        self._mgr = manager if manager is not None else get_cache_manager()
        self._fill_lock = fill_lock

    @property
    def spec_domain(self) -> str:
        return "conv"

    def _fill_lock_runtime(self) -> CacheFillLock | None:
        if self._fill_lock is not None:
            return self._fill_lock
        if not self._mgr.is_enabled():
            return None
        try:
            client = self._mgr._backend.client  # type: ignore[attr-defined]
        except RuntimeError:
            return None
        from app.core.config import get_settings

        ttl_ms = int(get_settings().cache_lock_ttl_ms)
        self._fill_lock = CacheFillLock(client, ttl_ms=ttl_ms)
        return self._fill_lock

    # ── List ──────────────────────────────────────────────────────────

    async def get_or_load_list(
        self,
        user_id: int | str,
        loader,
        *,
        query_hash: str = "all",
    ) -> ConversationListDTO | None:
        """Cache-aside list read with generation token.

        ``loader()`` is invoked only on cache miss; it must return a
        :class:`ConversationListDTO` (or ``None`` for empty / not-found).
        """
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec_domain):
            # Bypass path: just call the loader directly.
            return await loader()

        generation = await self.get_generation(user_id)
        cache_key = conv_list_key(user_id, generation, query_hash)

        # SingleFlight inside CacheManager coalesces concurrent miss
        # loads for the same generation+query.
        async def _adapt():
            dto = await loader()
            if dto is None:
                # Treat None as empty list (consistent with the bypass
                # empty semantics).  Avoid poisoning the cache with a
                # negative envelope for this hot read.
                return ConversationListDTO(summaries=[], total=0).to_dict()
            return dto.to_dict()

        async def _adapt_cached(raw):
            from app.cache.manager import _CacheMiss as _CM

            if isinstance(raw, _CM):
                return ConversationListDTO(summaries=[], total=0)
            if isinstance(raw, dict):
                return ConversationListDTO.from_dict(raw)
            return raw

        return await self._mgr.get_or_load(
            CONVERSATION_LIST_SPEC,
            cache_key,
            _adapt,
            fill_lock=self._fill_lock_runtime(),
            cached_adapter=_adapt_cached,
        )

    # ── Detail ───────────────────────────────────────────────────────

    async def get_or_load_detail(
        self,
        user_id: int | str,
        conv_public_id: str,
        loader,
    ) -> ConversationDetailDTO | None:
        """Cache-aside detail read (per-conv key, no generation)."""
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec_domain):
            return await loader()

        cache_key = conv_detail_key(user_id, conv_public_id)

        async def _adapt():
            dto = await loader()
            if dto is None:
                return None
            return dto.to_dict()

        async def _adapt_cached(raw):
            from app.cache.manager import _CacheMiss as _CM

            if isinstance(raw, _CM):
                return None
            if isinstance(raw, dict):
                return ConversationDetailDTO.from_dict(raw)
            return raw

        return await self._mgr.get_or_load(
            CONVERSATION_DETAIL_SPEC,
            cache_key,
            _adapt,
            fill_lock=None,  # detail miss not hot
            cached_adapter=_adapt_cached,
        )

    # ── Generation token (设计文档 §12.2) ─────────────────────────

    async def get_generation(self, user_id: int | str) -> str:
        """Return the per-user generation token.

        On first read (key absent) returns a freshly-generated token
        AND writes it back so subsequent readers see the same value.
        Token is random 16-hex so bump rotates to a different key segment.
        """
        from app.cache.manager import _CacheMiss as _CM

        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec_domain):
            # Bypass: caller still gets a stable token for this request.
            return self._generate_token()

        key = conv_generation_key(user_id)
        raw = await self._mgr.get(GENERATION_SPEC, key)
        if isinstance(raw, dict) and "token" in raw:
            return str(raw["token"])
        # Initialize on first read.
        token = self._generate_token()
        await self._mgr.set(GENERATION_SPEC, key, {"token": token})
        return token

    async def bump_generation(self, user_id: int | str) -> str:
        """Rotate the per-user generation token (设计文档 §12.3).

        After bump, any cached list under the OLD generation token is
        unreachable: the reader appends the new generation to its cache
        key, sees a different key, and re-loades from MySQL.

        Returns the new token.  No-op (returns a fresh in-memory token)
        when the domain is disabled / Redis is unavailable.
        """
        from app.core.config import get_settings

        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec_domain):
            return self._generate_token()

        key = conv_generation_key(user_id)
        token = self._generate_token()
        ok = await self._mgr.set(GENERATION_SPEC, key, {"token": token})
        if not ok:
            logger.debug(
                "ConversationCache.bump_generation: Redis SET failed "
                "user_id=%s; returning new token without persisting",
                user_id,
            )
        cache_metrics.record(
            domain=self.spec_domain, operation="bump_generation",
            result="hit" if ok else "error",
        )
        return token

    # ── Detail invalidation ─────────────────────────────────────────

    async def invalidate_detail(
        self,
        user_id: int | str,
        conv_public_id: str,
    ) -> bool:
        return await self._mgr.delete(
            CONVERSATION_DETAIL_SPEC,
            conv_detail_key(user_id, conv_public_id),
        )

    # ── helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _generate_token() -> str:
        """Random opaque token (hex). 16 hex chars ≈ 64 bits of entropy."""
        import secrets

        return secrets.token_hex(8)


# ── Module-level singleton ─────────────────────────────────────────


_cache: ConversationCache | None = None


def get_conversation_cache() -> ConversationCache:
    global _cache
    if _cache is None:
        _cache = ConversationCache()
    return _cache


def set_conversation_cache(c: ConversationCache | None) -> None:
    global _cache
    _cache = c


__all__ = [
    "CONVERSATION_DETAIL_SPEC",
    "CONVERSATION_LIST_SPEC",
    "ConversationCache",
    "ConversationDetailDTO",
    "ConversationListDTO",
    "GENERATION_SPEC",
    "conv_detail_key",
    "conv_generation_key",
    "conv_list_key",
    "get_conversation_cache",
    "set_conversation_cache",
]