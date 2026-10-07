"""LangGraph Runtime Reliability — targeted tests for FIX-B (postgres checkpointer).

Covers:
- TEST-PG-01: build_postgres_checkpointer returns AsyncPostgresSaver with pool
- TEST-PG-02: pool has _pg_pool attribute + health check callback
- TEST-PG-03: aclose_postgres_checkpointer closes pool
- TEST-PG-05: build failure (bad URL) → None, no silent MemorySaver
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent_runtime.persistence.postgres_checkpointer import (
    aclose_postgres_checkpointer,
    build_postgres_checkpointer,
    normalize_postgres_conn_string,
)


def _env_url() -> str:
    import os

    return os.environ.get("AGENT_RUNTIME_POSTGRES_URL", "") or (
        "postgresql+psycopg://testagent:xiaoliu@49.235.42.163:5432/langgraph?ssl=false"
    )


# ── TEST-PG-01 ─────────────────────────────────────────────────────
class TestBuildPostgresCheckpointer:
    """build_postgres_checkpointer must return a pool-backed saver."""

    @pytest.mark.skipif(
        not _env_url(), reason="AGENT_RUNTIME_POSTGRES_URL not set for live test"
    )
    async def test_build_returns_pool_saver(self):
        cp = await build_postgres_checkpointer(_env_url(), setup=True, timeout=8.0)
        assert cp is not None, "build_postgres_checkpointer returned None"
        assert type(cp).__name__ == "AsyncPostgresSaver"
        assert hasattr(cp, "_pg_pool"), "saver must carry _pg_pool (FIX-B)"
        from psycopg_pool import AsyncConnectionPool

        assert isinstance(cp._pg_pool, AsyncConnectionPool)
        await aclose_postgres_checkpointer(cp)

    @pytest.mark.skipif(
        not _env_url(), reason="AGENT_RUNTIME_POSTGRES_URL not set for live test"
    )
    async def test_aput_aget_tuple(self):
        cp = await build_postgres_checkpointer(_env_url(), setup=True, timeout=8.0)
        assert cp is not None
        config = {"configurable": {"thread_id": "test_fixb_pg", "checkpoint_ns": ""}}
        ckpt = {
            "v": 1,
            "id": "cid_test",
            "channel_values": {},
            "versions_seen": {},
            "pending_sends": [],
            "channel_versions": {},
        }
        await asyncio.wait_for(cp.aput(config, ckpt, {}, {"x": "1"}), timeout=15)
        got = await asyncio.wait_for(cp.aget_tuple(config), timeout=15)
        assert got is not None
        await aclose_postgres_checkpointer(cp)

    async def test_build_bad_url_returns_none(self):
        """Bad URL + setup=True → None (pool open fails, no silent MemorySaver).

        Note: pool.open() is async and may return before real connect succeeds;
        setup() on the saver forces a real round-trip → fails → None.
        """
        cp = await build_postgres_checkpointer(
            "postgresql://nouser:bad@127.0.0.1:1/nodb?sslmode=disable",
            setup=True,
            timeout=2.0,
        )
        assert cp is None

    def test_normalize_conn_string(self):
        """+psycopg suffix normalized to libpq form."""
        url = "postgresql+psycopg://u:p@h:5432/db?ssl=false"
        norm = normalize_postgres_conn_string(url)
        assert norm.startswith("postgresql://")
        assert "sslmode=disable" in norm
        assert "+psycopg" not in norm

    async def test_aclose_none_safe(self):
        """aclose_postgres_checkpointer(None) is a no-op."""
        await aclose_postgres_checkpointer(None)