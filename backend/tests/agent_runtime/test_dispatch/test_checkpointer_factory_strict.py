"""Phase 2.8R-C — Checkpointer 工厂严格化测试(4)。

设计目标(对应 docs/35 §3 + 验收四):
  * ``build_checkpointer(backend="memory")`` → MemorySaver 实例
  * ``build_checkpointer(backend="postgres", url="")`` → CheckpointerUnavailableError
  * ``build_checkpointer(backend=unknown)`` → CheckpointerUnavailableError(unknown_backend)
  * ``build_checkpointer(backend=None)`` 走 env var(env 不配 → memory)
"""

from __future__ import annotations

import os

import pytest

from app.core.exceptions import CheckpointerUnavailableError


@pytest.mark.asyncio
async def test_build_checkpointer_memory_returns_saver(monkeypatch):
    """backend=memory → MemorySaver 实例。"""
    from app.agent_runtime.persistence.checkpointer_factory import build_checkpointer

    cp = await build_checkpointer(backend="memory")
    # MemorySaver 是 langgraph.checkpoint.memory.MemorySaver
    from langgraph.checkpoint.memory import MemorySaver
    assert isinstance(cp, MemorySaver)


@pytest.mark.asyncio
async def test_build_checkpointer_postgres_no_url_raises(monkeypatch):
    """backend=postgres 但无 url → CheckpointerUnavailableError(reason=empty_postgres_url)。"""
    monkeypatch.delenv("AGENT_RUNTIME_POSTGRES_URL", raising=False)

    from app.agent_runtime.persistence.checkpointer_factory import build_checkpointer

    with pytest.raises(CheckpointerUnavailableError) as exc_info:
        await build_checkpointer(backend="postgres")
    assert exc_info.value.detail["reason"] == "empty_postgres_url"


@pytest.mark.asyncio
async def test_build_checkpointer_unknown_backend_raises():
    """backend='xxx' → CheckpointerUnavailableError(reason=unknown_backend)。"""
    from app.agent_runtime.persistence.checkpointer_factory import build_checkpointer

    with pytest.raises(CheckpointerUnavailableError) as exc_info:
        await build_checkpointer(backend="redis_only")
    assert exc_info.value.detail["reason"] == "unknown_backend"
    assert "redis_only" in str(exc_info.value.detail["backend"])


@pytest.mark.asyncio
async def test_build_checkpointer_default_to_memory(monkeypatch):
    """backend=None + 不设 env → 走 memory 兜底(纯单测用)。"""
    monkeypatch.delenv("AGENT_RUNTIME_CHECKPOINTER_BACKEND", raising=False)
    monkeypatch.delenv("AGENT_RUNTIME_POSTGRES_URL", raising=False)

    from app.agent_runtime.persistence.checkpointer_factory import build_checkpointer
    from langgraph.checkpoint.memory import MemorySaver

    cp = await build_checkpointer()
    assert isinstance(cp, MemorySaver)


@pytest.mark.asyncio
async def test_build_postgres_checkpointer_probe_unreachable(monkeypatch):
    """backend=postgres + url 但连不上 → CheckpointerUnavailableError(probe_unhealthy)。

    内部 probe 返回 False(不抛),我代码再抛 probe_unhealthy。
    """
    from app.agent_runtime.persistence.checkpointer_factory import (
        build_postgres_checkpointer,
    )

    bad_url = "postgresql+psycopg://nope:nope@127.0.0.1:1/none"

    # 实测在 Windows 上 probe 内部会超时或连接拒绝,经实测返回 False
    # 我们也接受抛错(probe_failed)的兜底
    with pytest.raises(CheckpointerUnavailableError) as exc_info:
        await build_postgres_checkpointer(bad_url, timeout=1.0)
    # detail 必含 reason;任何 probe 失败 reason 都可
    assert "reason" in (exc_info.value.detail or {})


__all__ = [
    "test_build_checkpointer_memory_returns_saver",
    "test_build_checkpointer_postgres_no_url_raises",
    "test_build_checkpointer_unknown_backend_raises",
    "test_build_checkpointer_default_to_memory",
    "test_build_postgres_checkpointer_probe_unreachable",
]
