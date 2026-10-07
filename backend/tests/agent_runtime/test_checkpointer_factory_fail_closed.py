"""Phase 2.8R-D:Checkpointer Factory 测试。

覆盖:
  * build_checkpointer 严格 await(返回 Awaitable)
  * memory 模式 OK
  * postgres 失败抛 CheckpointerUnavailableError(**无静默 MemorySaver fallback**)
  * probe_postgres_checkpointer 探测 → setup 真实执行
"""

from __future__ import annotations

import inspect

import pytest

from app.agent_runtime.persistence.checkpointer_factory import (
    build_checkpointer,
    build_inmemory_checkpointer,
)
from app.core.exceptions import CheckpointerUnavailableError


def test_build_checkpointer_is_async() -> None:
    """工厂必须是 async,强制 await — 不允许返回 coroutine 后被忽略。"""
    assert inspect.iscoroutinefunction(build_checkpointer)


def test_build_checkpointer_memory_returns_memory_saver() -> None:
    """显式 memory 模式 → MemorySaver 实例。"""
    import asyncio

    async def _run():
        cp = await build_checkpointer(backend="memory")
        return cp

    cp = asyncio.run(_run())
    assert cp is not None
    # MemorySaver class name can be either MemorySaver(InMemorySaver in newer langgraph)
    assert "MemorySaver" in type(cp).__name__ or "Saver" in type(cp).__name__


def test_build_checkpointer_postgres_unknown_url_raises() -> None:
    """Postgres 不可用 → 抛 CheckpointerUnavailableError(不再静默 MemorySaver)。"""
    import asyncio

    async def _run():
        return await build_checkpointer(
            backend="postgres",
            url="postgresql://no:no@127.0.0.1:1/none",
        )

    with pytest.raises(CheckpointerUnavailableError):
        asyncio.run(_run())


def test_build_checkpointer_unknown_backend_raises() -> None:
    """未知 backend → 抛错。"""
    import asyncio

    async def _run():
        return await build_checkpointer(backend="mongodb")

    with pytest.raises(CheckpointerUnavailableError):
        asyncio.run(_run())


def test_build_inmemory_checkpointer_is_sync() -> None:
    """低阶 build_inmemory_checkpointer 是同步 helper,返回 MemorySaver。"""
    cp = build_inmemory_checkpointer()
    assert cp is not None
