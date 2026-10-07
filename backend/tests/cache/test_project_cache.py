"""Project cache keys must preserve owner and workspace isolation."""

from app.cache.domains.project_cache import (
    ProjectCache,
    project_context_key,
    project_detail_key,
    project_list_filter_hash,
    project_list_key,
)
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_project_detail_cache_key_contains_user_and_project():
    key = project_detail_key(7, "prj_alpha")
    assert ":user:7:" in key
    assert ":project:prj_alpha" in key


def test_project_list_cache_hash_changes_with_query_scope_and_page():
    one = project_list_filter_hash("alpha", "all", 1, 20)
    assert one != project_list_filter_hash("beta", "all", 1, 20)
    assert one != project_list_filter_hash("alpha", "shared", 1, 20)
    assert one != project_list_filter_hash("alpha", "all", 2, 20)
    assert ":user:7:" in project_list_key(7, "generation", one)


def test_project_context_cache_key_contains_owner_project_generation_and_query_hash():
    key = project_context_key(7, "prj_alpha", "g1", "query123")
    assert ":user:7:" in key
    assert ":project:prj_alpha:" in key
    assert ":g:g1:q:query123" in key
    assert key != project_context_key(8, "prj_alpha", "g1", "query123")
    assert key != project_context_key(7, "prj_beta", "g1", "query123")


@pytest.mark.asyncio
async def test_project_cache_invalidation_bypasses_disabled_manager():
    manager = MagicMock()
    manager.is_enabled.return_value = False
    manager.bump_generation = AsyncMock()
    manager.delete = AsyncMock()

    await ProjectCache(manager).invalidate(7, "prj_alpha")

    manager.bump_generation.assert_not_awaited()
    manager.delete.assert_not_awaited()
