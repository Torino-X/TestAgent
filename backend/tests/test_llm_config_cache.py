"""Tests for the per-user LLMConfigCache (F015).

The cache lives at ``app.core.llm_config_cache.llm_config_cache`` and
backs the per-user model configuration lifecycle:

  - login  → ``SettingsService.bootstrap_user_config`` → ``set``
  - logout → ``llm_config_cache.invalidate``
  - tool   → ``get_or_load`` (lazy load on miss)
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.llm_config_cache import LLMConfigCache
from app.services.settings_service import LLMConfigProvider


def _provider(uid: int) -> LLMConfigProvider:
    """Build a stub provider whose api_url encodes the user_id for assertions."""
    return LLMConfigProvider(
        api_url=f"https://api.test/{uid}/v1",
        api_key=f"sk-user-{uid}",
        model_name=f"model-{uid}",
        timeout=120,
        enable_thinking=False,
    )


@pytest.mark.asyncio
async def test_set_and_get_or_load_return_same_object():
    cache = LLMConfigCache()
    p = _provider(1)
    await cache.set(1, p)
    loader_called = False

    async def loader(uid: int):
        nonlocal loader_called
        loader_called = True
        return None

    result = await cache.get_or_load(1, loader)
    assert result is p
    assert not loader_called  # cache hit — loader not invoked


@pytest.mark.asyncio
async def test_invalidate_forces_reload():
    cache = LLMConfigCache()
    p1 = _provider(1)
    await cache.set(1, p1)

    call_count = 0

    async def loader(uid: int):
        nonlocal call_count
        call_count += 1
        return _provider(2)  # return a different provider

    # First call: cache hit
    result1 = await cache.get_or_load(1, loader)
    assert result1 is p1
    assert call_count == 0

    # Invalidate
    await cache.invalidate(1)

    # Second call: cache miss → loader invoked
    result2 = await cache.get_or_load(1, loader)
    assert result2.api_url == "https://api.test/2/v1"
    assert call_count == 1

    # Third call: cache hit
    result3 = await cache.get_or_load(1, loader)
    assert result3 is result2
    assert call_count == 1


@pytest.mark.asyncio
async def test_get_or_load_does_not_cache_none():
    """A None result (user not configured) must be re-queried next call."""
    cache = LLMConfigCache()
    call_count = 0

    async def loader(uid: int):
        nonlocal call_count
        call_count += 1
        return None

    result1 = await cache.get_or_load(42, loader)
    result2 = await cache.get_or_load(42, loader)
    assert result1 is None
    assert result2 is None
    assert call_count == 2  # loader invoked both times
    assert 42 not in cache.cached_user_ids()


@pytest.mark.asyncio
async def test_clear_wipes_every_user():
    cache = LLMConfigCache()
    await cache.set(1, _provider(1))
    await cache.set(2, _provider(2))
    assert sorted(cache.cached_user_ids()) == [1, 2]
    await cache.clear()
    assert cache.cached_user_ids() == []


@pytest.mark.asyncio
async def test_concurrent_get_or_load_calls_loader_once():
    """Lock must serialise the loader so it only runs once per user."""
    cache = LLMConfigCache()
    call_count = 0

    async def slow_loader(uid: int):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)  # yield to let siblings race
        return _provider(uid)

    # Fire 5 concurrent gets for the same user
    results = await asyncio.gather(*(cache.get_or_load(7, slow_loader) for _ in range(5)))
    assert call_count == 1
    # All five should return the same object (cached after first).
    for r in results[1:]:
        assert r is results[0]
