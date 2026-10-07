"""Library Metadata Cache (设计文档 §14 + 提示词 §25).

消除每次 GET /api/library/items 都跑 ``list_by_user`` + ``list_deleted_by_user``
+ artifact list 的重复 DB 读. 文件 metadata 仅 — **不缓存 OSS binary**.

设计要点 (设计文档 §14.1-§14.4):

  Key:      ta:{env}:cache:v1:lib:list:user:{uid}:g:{generation}:f:{filter_hash}
  TTL:      60s / negative 30s
  Generation Key: ta:{env}:cache:v1:gen:lib:user:{uid}

写穿触发 (设计文档 §14.3 + 提示词 §25):
  - upload                → 新行可见, bump
  - rename                → 行内容变, bump
  - soft_delete           → list active 排除, bump
  - restore               → list active 重新包含, bump
  - permanent_delete      → 物理删除, bump
  - artifact create       → bump
  - artifact rename       → bump
  - artifact delete       → bump
  - artifact restore      → bump

bump_generation 改 token 让旧 list 不可达, 60s TTL 内自然过期, 无 SCAN.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.cache.distributed_lock import CacheFillLock
from app.cache.key_builder import build_key, hash_filter
from app.cache.manager import CacheManager, get_cache_manager
from app.cache.metrics import cache_metrics
from app.cache.specs import CacheSpec

logger = logging.getLogger(__name__)


# ── Spec ────────────────────────────────────────────────────────────


# Per 设计文档 §14.4: TTL 60s / negative 30s
LIBRARY_LIST_SPEC = CacheSpec(
    domain="lib",
    ttl_seconds=60,
    negative_ttl_seconds=30,
    jitter_ratio=0.10,
    enable_singleflight=True,
    enable_distributed_fill_lock=True,
)

GENERATION_SPEC = CacheSpec(
    domain="lib",
    ttl_seconds=60 * 60 * 24 * 30,
    jitter_ratio=0.0,
    enable_singleflight=False,
    enable_distributed_fill_lock=False,
)


# ── DTO ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LibraryListDTO:
    """Cached library list response shape."""

    items: list[dict[str, Any]]
    total: int

    def to_dict(self) -> dict[str, Any]:
        return {"items": list(self.items), "total": self.total}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LibraryListDTO":
        return cls(
            items=list(data.get("items", [])),
            total=int(data.get("total", 0)),
        )


# ── Key helpers ────────────────────────────────────────────────────


def library_list_key(user_id: int | str, generation: str, filter_hash: str = "all") -> str:
    return build_key("lib", "list", "user", user_id, "g", generation, "q", filter_hash)


def library_generation_key(user_id: int | str) -> str:
    return build_key("gen", "lib", "user", user_id)


def filter_hash_for(
    *,
    category: str = "all",
    query: str = "",
    scope: str = "active",
    source: str = "all",
    file_type: str = "all",
    page: int = 1,
    page_size: int = 50,
) -> str:
    """Stable canonical hash of library filter parameters (设计文档 §7)."""
    return hash_filter(category, query, scope, source, file_type, page, page_size)


# ── Cache service ─────────────────────────────────────────────────


class LibraryCache:
    """Per-user library metadata list cache with generation token (设计文档 §14).

    Mirrors :class:`ConversationCache` for list semantics — generation
    token + filter hash forms the list cache key; bumping generation
    invalidates all old list entries without SCAN.
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
        return "lib"

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

    # ── List cache-aside ─────────────────────────────────────────────

    async def get_or_load_list(
        self,
        user_id: int | str,
        loader,
        *,
        filter_hash: str = "all",
    ) -> LibraryListDTO:
        """Cache-aside list read with generation token.

        ``loader()`` is called on cache miss; it must return
        :class:`LibraryListDTO` (never None — empty result is
        ``LibraryListDTO(items=[], total=0)``).
        """
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec_domain):
            return await loader()

        generation = await self.get_generation(user_id)
        cache_key = library_list_key(user_id, generation, filter_hash)

        async def _adapt():
            dto = await loader()
            return dto.to_dict()

        async def _adapt_cached(raw):
            from app.cache.manager import _CacheMiss as _CM

            if isinstance(raw, _CM):
                return LibraryListDTO(items=[], total=0)
            if isinstance(raw, dict):
                return LibraryListDTO.from_dict(raw)
            return raw

        return await self._mgr.get_or_load(
            LIBRARY_LIST_SPEC,
            cache_key,
            _adapt,
            fill_lock=self._fill_lock_runtime(),
            cached_adapter=_adapt_cached,
        )

    # ── Generation token ────────────────────────────────────────────

    async def get_generation(self, user_id: int | str) -> str:
        """Return the per-user generation token."""
        from app.cache.manager import _CacheMiss as _CM

        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec_domain):
            return self._generate_token()

        key = library_generation_key(user_id)
        raw = await self._mgr.get(GENERATION_SPEC, key)
        if isinstance(raw, dict) and "token" in raw:
            return str(raw["token"])
        token = self._generate_token()
        await self._mgr.set(GENERATION_SPEC, key, {"token": token})
        return token

    async def bump_generation(self, user_id: int | str) -> str:
        """Rotate generation token (设计文档 §14.3).

        Call after every library mutation: upload / rename / soft_delete /
        restore / permanent_delete / artifact create/rename/delete/restore.
        """
        from app.core.config import get_settings

        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec_domain):
            return self._generate_token()

        key = library_generation_key(user_id)
        token = self._generate_token()
        ok = await self._mgr.set(GENERATION_SPEC, key, {"token": token})
        if not ok:
            logger.debug(
                "LibraryCache.bump_generation: Redis SET failed "
                "user_id=%s; returning new token without persisting",
                user_id,
            )
        cache_metrics.record(
            domain=self.spec_domain, operation="bump_generation",
            result="hit" if ok else "error",
        )
        return token

    @staticmethod
    def _generate_token() -> str:
        import secrets

        return secrets.token_hex(8)


# ── Module-level singleton ─────────────────────────────────────────


_cache: LibraryCache | None = None


def get_library_cache() -> LibraryCache:
    global _cache
    if _cache is None:
        _cache = LibraryCache()
    return _cache


def set_library_cache(c: LibraryCache | None) -> None:
    global _cache
    _cache = c


__all__ = [
    "GENERATION_SPEC",
    "LIBRARY_LIST_SPEC",
    "LibraryCache",
    "LibraryListDTO",
    "filter_hash_for",
    "get_library_cache",
    "library_generation_key",
    "library_list_key",
    "set_library_cache",
]
