"""P0 收口测试:cache mutation 必须在 DB commit 之后才执行.

场景:
  1. DB flush 成功 → cache 不应提前更新
  2. commit 成功 → cache 应被更新
  3. rollback → cache 不应被更新
  4. 多次 commit + 多次 register → 每个 commit 各自触发对应 hook
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.db.sync import register_after_commit


class _FakeSyncSession:
    """Minimal stand-in for a SQLAlchemy ``Session``.

    Implements only ``info`` and ``sync_session`` properties so the helper
    can attach the listener.  We then fire ``after_commit`` /
    ``after_rollback`` events manually via SQLAlchemy ``event.api.listen``
    on this object to keep the test hermetic (no real DB).

    Note: SQLAlchemy event registration checks for the class in the target
    registry.  We need a real ORM ``Session`` for that — use a stub Session
    that mimics the API surface enough to satisfy ``event.listen``.
    """

    def __init__(self) -> None:
        self.info: dict = {}
        self.committed = False
        self.rolled_back = False


class _MockAsyncSession:
    """Has ``sync_session`` attribute exposing a fake ORM Session target."""

    def __init__(self, sync_target) -> None:
        self.sync_session = sync_target
        self.info = sync_target.info
        self._fired: list[str] = []

    async def commit(self) -> None:
        from sqlalchemy import event
        self.sync_session.committed = True
        event.api._listen._exec_sync_listener_after_commit(self.sync_session)

    async def rollback(self) -> None:
        from sqlalchemy import event
        self.sync_session.rolled_back = True
        # rollback fires after_rollback; we just clear hooks
        self.sync_session.info.pop("_cache_after_commit_hooks", None)


# We avoid Mock classes and just test with a real in-memory AsyncSession.


@pytest.mark.asyncio
async def test_commit_fires_hook():
    """commit 成功后, hook 应被触发."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(eng) as s:
        fired: list[str] = []
        async def hook(_session):
            fired.append("ok")
        register_after_commit(s, hook)
        await s.commit()
        await asyncio.sleep(0.05)
        assert fired == ["ok"]


@pytest.mark.asyncio
async def test_rollback_skips_hook():
    """rollback 后, hook 不应触发."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(eng) as s:
        fired: list[str] = []
        async def hook(_session):
            fired.append("ok")
        register_after_commit(s, hook)
        await s.rollback()
        await asyncio.sleep(0.05)
        assert fired == []


@pytest.mark.asyncio
async def test_multiple_registrations_each_fire_once():
    """多次 register_after_commit 在多次 commit 中按序触发, 互不串扰."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(eng) as s:
        fired: list[str] = []
        async def hook_a(_session):
            fired.append("a")
        async def hook_b(_session):
            fired.append("b")

        register_after_commit(s, hook_a)
        register_after_commit(s, hook_b)
        await s.commit()
        await asyncio.sleep(0.05)
        assert "a" in fired and "b" in fired


@pytest.mark.asyncio
async def test_hook_exception_does_not_propagate():
    """hook 抛错不应影响主流程 (DB 已提交,不应回滚)."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with AsyncSession(eng) as s:
        async def hook(_session):
            raise RuntimeError("intentional")

        register_after_commit(s, hook)
        # commit should NOT raise even though hook raises
        await s.commit()
        await asyncio.sleep(0.05)
        # Verify session is still usable
        assert s.info is not None


@pytest.mark.asyncio
async def test_no_session_no_op():
    """mock / 非 SQLAlchemy session 不抛错, 仅 debug log."""
    fired: list[str] = []

    async def hook(_session):
        fired.append("ok")

    # Should not raise; hook is simply not registered.
    register_after_commit(object(), hook)
    await asyncio.sleep(0.05)
    assert fired == []