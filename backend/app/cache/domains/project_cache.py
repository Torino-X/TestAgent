"""Owner-isolated Project list/detail cache."""

from __future__ import annotations

from typing import Any

from app.cache.key_builder import build_key, hash_filter
from app.cache.manager import CacheManager, get_cache_manager
from app.cache.specs import CacheSpec


PROJECT_LIST_SPEC = CacheSpec(domain="project", ttl_seconds=60, negative_ttl_seconds=15, jitter_ratio=0.1)
PROJECT_DETAIL_SPEC = CacheSpec(domain="project", ttl_seconds=60, negative_ttl_seconds=15, jitter_ratio=0.1)
PROJECT_CONTEXT_SPEC = CacheSpec(domain="project", ttl_seconds=45, negative_ttl_seconds=10, jitter_ratio=0.1)


def project_list_filter_hash(query: str, scope: str, page: int, page_size: int) -> str:
    return hash_filter(query.strip().casefold(), scope, page, page_size)


def project_list_key(user_id: int | str, generation: str, query_hash: str) -> str:
    return build_key("project", "list", "user", user_id, "g", generation, "q", query_hash)


def project_detail_key(user_id: int | str, project_public_id: str) -> str:
    return build_key("project", "detail", "user", user_id, "project", project_public_id)


def project_context_key(
    user_id: int | str,
    project_public_id: str,
    generation: str,
    query_hash: str,
) -> str:
    """Owner + Project + query isolated context-cache key."""
    return build_key(
        "project", "context", "user", user_id, "project", project_public_id,
        "g", generation, "q", query_hash,
    )


class ProjectCache:
    def __init__(self, manager: CacheManager | None = None) -> None:
        self._manager = manager or get_cache_manager()

    async def get_or_load_list(
        self,
        user_id: int | str,
        *,
        query: str,
        scope: str,
        page: int,
        page_size: int,
        loader,
    ) -> dict[str, Any]:
        if not self._manager.is_enabled() or not self._manager.domain_enabled("project"):
            return await loader()
        generation = await self._manager.get_generation("project", user_id)
        key = project_list_key(
            user_id,
            generation,
            project_list_filter_hash(query, scope, page, page_size),
        )
        result = await self._manager.get_or_load(PROJECT_LIST_SPEC, key, loader)
        return result or {"items": [], "total": 0}

    async def get_or_load_detail(self, user_id: int | str, project_public_id: str, loader):
        if not self._manager.is_enabled() or not self._manager.domain_enabled("project"):
            return await loader()
        return await self._manager.get_or_load(
            PROJECT_DETAIL_SPEC,
            project_detail_key(user_id, project_public_id),
            loader,
        )

    async def get_or_load_context(
        self,
        user_id: int | str,
        project_public_id: str,
        *,
        query: str,
        loader,
    ):
        if not self._manager.is_enabled() or not self._manager.domain_enabled("project"):
            return await loader()
        generation = await self._manager.get_generation("project", user_id)
        key = project_context_key(
            user_id,
            project_public_id,
            generation,
            hash_filter(query.strip().casefold()),
        )
        return await self._manager.get_or_load(PROJECT_CONTEXT_SPEC, key, loader)

    async def invalidate(self, user_id: int | str, project_public_id: str | None = None) -> None:
        if not self._manager.is_enabled() or not self._manager.domain_enabled("project"):
            return
        await self._manager.bump_generation("project", user_id)
        if project_public_id:
            await self._manager.delete(
                PROJECT_DETAIL_SPEC,
                project_detail_key(user_id, project_public_id),
            )


_cache: ProjectCache | None = None


def get_project_cache() -> ProjectCache:
    global _cache
    if _cache is None:
        _cache = ProjectCache()
    return _cache


def set_project_cache(cache: ProjectCache | None) -> None:
    global _cache
    _cache = cache
