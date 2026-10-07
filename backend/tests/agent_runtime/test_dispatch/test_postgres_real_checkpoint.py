"""Phase 2.8B — 真实 Postgres checkpointer 4 表落库实证(env-gated)。

需要的测试环境:
* ``POSTGRES_TEST_URL`` 环境变量(CI / staging 设;dev 不设则整组 skip)
* ``psycopg[binary]`` + ``langgraph-checkpoint-postgres`` 已在 requirements.txt

守禁令映射:
* #24 — Postgres 不健康时不能写入生产路径(本测试本身只读/写,验证 setup 行为)
"""

from __future__ import annotations

import os

import pytest

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL", "")

pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL not set; skipping real Postgres tests",
)


async def test_setup_creates_four_core_tables() -> None:
    """``cp.setup()`` 真实创建 4 张核心表(checkpoints / blobs / writes / migrations)。

    通过 ``verify_postgres_4_tables`` 验证 — 缺表时抛 RuntimeError。
    """
    from app.agent_runtime.persistence.postgres_checkpointer import (
        build_postgres_checkpointer,
        aclose_postgres_checkpointer,
    )
    from app.agent_runtime.persistence.postgres_integration import (
        verify_postgres_4_tables,
        EXPECTED_CORE_TABLES,
    )

    cp = await build_postgres_checkpointer(POSTGRES_TEST_URL, setup=True)
    try:
        assert cp is not None, "build_postgres_checkpointer 在 POSTGRES_TEST_URL 设置时应返回非 None"
        present = await verify_postgres_4_tables(cp)
        assert EXPECTED_CORE_TABLES.issubset(set(present)), (
            f"4 张核心表必须都在;missing={EXPECTED_CORE_TABLES - set(present)}"
        )
    finally:
        if cp is not None:
            await aclose_postgres_checkpointer(cp)


async def test_verify_postgres_4_tables_returns_sorted_list() -> None:
    """``verify_postgres_4_tables`` 返回 sorted list(契约:调用方无需 sort)。"""
    from app.agent_runtime.persistence.postgres_checkpointer import (
        build_postgres_checkpointer,
        aclose_postgres_checkpointer,
    )
    from app.agent_runtime.persistence.postgres_integration import (
        verify_postgres_4_tables,
    )

    cp = await build_postgres_checkpointer(POSTGRES_TEST_URL, setup=False)
    try:
        assert cp is not None
        present = await verify_postgres_4_tables(cp)
        # 必须 sorted
        assert present == sorted(present), f"返回必须是 sorted;got={present}"
        # 至少 4 张核心表
        assert len(present) >= 4
    finally:
        if cp is not None:
            await aclose_postgres_checkpointer(cp)
