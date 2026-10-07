"""Small Redis cache for preview state; PDF bytes always remain in private OSS."""

from __future__ import annotations

from typing import Any

from app.cache.key_builder import build_key
from app.cache.manager import CacheManager, _CacheMiss, get_cache_manager
from app.cache.specs import CacheSpec


PREVIEW_STATUS_SPEC = CacheSpec(
    domain="lib",
    ttl_seconds=300,
    jitter_ratio=0.10,
    enable_singleflight=False,
    enable_distributed_fill_lock=False,
)


def preview_status_key(user_id: int | str, item_id: str) -> str:
    return build_key("lib", "preview", "status", "user", user_id, "item", item_id)


class DocumentPreviewCache:
    """Caches only serialized status/path metadata, never binary content."""

    def __init__(self, manager: CacheManager | None = None) -> None:
        self._mgr = manager if manager is not None else get_cache_manager()

    async def get(self, user_id: int | str, item_id: str) -> dict[str, Any] | None:
        cached = await self._mgr.get(PREVIEW_STATUS_SPEC, preview_status_key(user_id, item_id))
        return dict(cached) if isinstance(cached, dict) and not isinstance(cached, _CacheMiss) else None

    async def set(self, user_id: int | str, item_id: str, status: dict[str, Any]) -> None:
        await self._mgr.set(PREVIEW_STATUS_SPEC, preview_status_key(user_id, item_id), status)

    async def delete(self, user_id: int | str, item_id: str) -> None:
        await self._mgr.delete(PREVIEW_STATUS_SPEC, preview_status_key(user_id, item_id))


_cache: DocumentPreviewCache | None = None


def get_document_preview_cache() -> DocumentPreviewCache:
    global _cache
    if _cache is None:
        _cache = DocumentPreviewCache()
    return _cache


def set_document_preview_cache(cache: DocumentPreviewCache | None) -> None:
    global _cache
    _cache = cache
