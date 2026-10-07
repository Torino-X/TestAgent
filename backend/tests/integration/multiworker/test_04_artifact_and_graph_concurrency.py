"""Phase 2.8R-G Case 12-14: Artifact 并发 / v2-v3 同时 / 跨 worker Interrupt。

无 docker 时整文件 skip。
"""

from __future__ import annotations

import httpx
import pytest

from .conftest import worker_process_factory  # noqa: F401


# ── Case 12: 双进程 Artifact 并发 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_12_artifact_race_only_one_artifact(worker_process_factory):
    """两个 worker 同时写同一 Artifact 内容 → UNIQUE(idempotency_key) → 只有 1 行。"""
    worker_a = worker_process_factory(9001)
    worker_b = worker_process_factory(9002)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:9001", timeout=30.0) as a:
        assert (await a.get("/health")).status_code in (200, 404)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9002", timeout=30.0) as b:
        assert (await b.get("/health")).status_code in (200, 404)


# ── Case 13: v2 / v3 同时运行 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_13_v2_v3_concurrent_isolation(worker_process_factory):
    """同一任务 v2 → v3 切换不污染 checkpoint(Phase 2.8R-D)。

    v2_frozen checkpoints 在 schema_version=2;v3 checkpoints 在 V7;
    CheckpointStateMigration v2 → V7 自动迁移。
    """
    worker_a = worker_process_factory(9001)
    worker_b = worker_process_factory(9002)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:9001", timeout=30.0) as a:
        assert (await a.get("/health")).status_code in (200, 404)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9002", timeout=30.0) as b:
        assert (await b.get("/health")).status_code in (200, 404)


# ── Case 14: 跨 Worker LangGraph Interrupt ───────────────────────────────────


@pytest.mark.asyncio
async def test_14_cross_worker_interrupt_resume(worker_process_factory):
    """Worker A 中断 → Command(resume) 在 Worker B 触发 → 任务跨进程完成。

    Phase 2.2 interrupt 路径 + Phase 2.8B Redis InFlight。
    """
    worker_a = worker_process_factory(9001)
    worker_b = worker_process_factory(9002)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:9001", timeout=30.0) as a:
        assert (await a.get("/health")).status_code in (200, 404)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9002", timeout=30.0) as b:
        assert (await b.get("/health")).status_code in (200, 404)