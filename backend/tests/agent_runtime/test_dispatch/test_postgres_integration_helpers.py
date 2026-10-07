"""Phase 2.8B — ``postgres_integration`` helpers 单元测试(无需真实 Postgres)。

验证 ``verify_postgres_4_tables`` / ``roundtrip_checkpoint`` / ``count_checkpoints``
对 None cp / AttributeError 等边界条件的处理 — 不依赖真实 PG 即可测。
"""

from __future__ import annotations

import pytest

from app.agent_runtime.persistence.postgres_integration import (
    EXPECTED_CORE_TABLES,
    count_checkpoints,
    roundtrip_checkpoint,
    verify_postgres_4_tables,
)


def test_expected_core_tables_constants() -> None:
    """``EXPECTED_CORE_TABLES`` 必须包含 4 张核心表名。"""
    expected = {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
    assert EXPECTED_CORE_TABLES == expected
    assert isinstance(EXPECTED_CORE_TABLES, frozenset)


async def test_verify_postgres_4_tables_raises_on_empty_dsn() -> None:
    """``dsn=None`` 时 ``verify_postgres_4_tables`` 抛 RuntimeError。"""
    with pytest.raises(RuntimeError, match="DSN 为空"):
        await verify_postgres_4_tables(dsn=None)


async def test_roundtrip_checkpoint_raises_on_none() -> None:
    """``cp=None`` 时 ``roundtrip_checkpoint`` 抛 RuntimeError。"""
    with pytest.raises(RuntimeError, match="cp is None"):
        await roundtrip_checkpoint(None, thread_id="t", payload={})


async def test_count_checkpoints_raises_on_none() -> None:
    """``cp=None`` 时 ``count_checkpoints`` 抛 RuntimeError。"""
    with pytest.raises(RuntimeError, match="cp is None"):
        await count_checkpoints(None, thread_id="t")


async def test_verify_postgres_4_tables_raises_when_no_dsn() -> None:
    """Phase 2.8R-D:verify_postgres_4_tables 接受 dsn: str,空字符串报错。"""
    with pytest.raises(RuntimeError, match="DSN 为空"):
        await verify_postgres_4_tables(dsn="")


async def test_verify_postgres_4_tables_raises_when_missing() -> None:
    """Phase 2.8R-D:模拟 psycopg 连接查 pg_tables 只返回部分表 → RuntimeError。

    用 monkeypatch 替换 psycopg.AsyncConnection.connect 返回的 fake conn。
    此测试在无 libpq 的 Windows 环境也能运行(mock 掉 psycopg import)。
    """
    import sys
    import types
    import unittest.mock as mock
    from contextlib import asynccontextmanager

    class _FakeCursor:
        def __init__(self):
            self._fetched = False

        async def execute(self, sql):
            return None

        async def fetchall(self):
            self._fetched = True
            # 只返回 1 张核心表(缺 3 张)
            return [("checkpoints",)]

    @asynccontextmanager
    async def _cursor_ctx():
        cur = _FakeCursor()
        yield cur

    class _FakeAsyncConnection:
        @staticmethod
        async def connect(dsn, **kwargs):
            return _FakeRawConn()

    class _FakeRawConn:
        def cursor(self):
            return _cursor_ctx()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    # 构造 fake psycopg 模块,避免真实 import 在无 libpq 环境失败
    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.AsyncConnection = _FakeAsyncConnection  # type: ignore[attr-defined]
    # 注入到 sys.modules,使 verify_postgres_4_tables 内部 import psycopg 能拿到 fake
    with mock.patch.dict(sys.modules, {"psycopg": fake_psycopg}):
        with pytest.raises(RuntimeError, match="missing"):
            await verify_postgres_4_tables(dsn="postgresql://fake:fake@127.0.0.1:1/fake")
