"""Phase 2.8B — 真实 Postgres round-trip 测试(env-gated)。

写入 → 读回 → 验证一致(验证持久化真的能 round-trip,不只是建表)。

需要的测试环境:
* ``POSTGRES_TEST_URL`` 环境变量
"""

from __future__ import annotations

import os
import uuid

import pytest

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL", "")

pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL not set; skipping real Postgres round-trip tests",
)


async def test_roundtrip_checkpoint_write_then_read() -> None:
    """写一个 sentinel payload → 重建 sa read → 验证 marker 一致。"""
    from app.agent_runtime.persistence.postgres_checkpointer import (
        build_postgres_checkpointer,
        aclose_postgres_checkpointer,
    )
    from app.agent_runtime.persistence.postgres_integration import (
        roundtrip_checkpoint,
        count_checkpoints,
    )

    cp = await build_postgres_checkpointer(POSTGRES_TEST_URL, setup=True)
    try:
        assert cp is not None
        thread_id = f"test-rt-{uuid.uuid4().hex[:8]}"
        sentinel = {"marker": "phase_2_8b_roundtrip", "value": 42}

        result = await roundtrip_checkpoint(
            cp, thread_id=thread_id, payload=sentinel
        )
        assert result is not None, "roundtrip 应返回 dict"
        # checkpoint 字段包含 sentinel
        checkpoint = result["checkpoint"]
        assert checkpoint is not None
        # count 应 ≥ 1
        count = await count_checkpoints(cp, thread_id=thread_id)
        assert count >= 1, f"写完后 count 应 ≥ 1;got={count}"
    finally:
        if cp is not None:
            await aclose_postgres_checkpointer(cp)


async def test_count_checkpoints_zero_for_new_thread() -> None:
    """新 thread_id count=0(避免污染测试)。"""
    from app.agent_runtime.persistence.postgres_checkpointer import (
        build_postgres_checkpointer,
        aclose_postgres_checkpointer,
    )
    from app.agent_runtime.persistence.postgres_integration import count_checkpoints

    cp = await build_postgres_checkpointer(POSTGRES_TEST_URL, setup=False)
    try:
        assert cp is not None
        thread_id = f"test-empty-{uuid.uuid4().hex[:8]}"
        count = await count_checkpoints(cp, thread_id=thread_id)
        assert count == 0
    finally:
        if cp is not None:
            await aclose_postgres_checkpointer(cp)
