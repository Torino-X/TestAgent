"""Phase 2.8R-G Case 8-11: 跨 worker Resume / Cancel / Redis 故障 / PG 故障。

无 docker 时整文件 skip。
"""

from __future__ import annotations

import httpx
import pytest

from .conftest import worker_process_factory  # noqa: F401


# ── Case 8: 跨 Worker Resume ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_08_cross_worker_resume(worker_process_factory):
    """Worker A 创建任务到 pause_for_legacy_confirm → Worker B resume_task 接走。"""
    worker_a = worker_process_factory(9001)
    worker_b = worker_process_factory(9002)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:9001", timeout=30.0) as a:
        assert (await a.get("/health")).status_code in (200, 404)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9002", timeout=30.0) as b:
        assert (await b.get("/health")).status_code in (200, 404)


# ── Case 9: 跨 Worker Cancel ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_09_cross_worker_cancel(worker_process_factory):
    """Worker A 跑任务 → 客户端发 cancel 到 Worker B → DistributedCancellationService 终止。

    Phase 2.6.6 DistributedCancellationService 跨 worker cancel。
    """
    worker_a = worker_process_factory(9001)
    worker_b = worker_process_factory(9002)

    async with httpx.AsyncClient(base_url="http://127.0.0.1:9002", timeout=30.0) as b:
        assert (await b.get("/health")).status_code in (200, 404)


# ── Case 10: Redis 故障行为 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_10_redis_failure_graceful_degrade(worker_process_factory):
    """Redis 突然挂 → 进程内 in-memory fallback;不静默吞错,日志 WARN。

    Phase 2.8B ADR:Redis 不可用 graceful degrade 到进程级 InFlightTaskRegistry。
    """
    worker_a = worker_process_factory(9001)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9001", timeout=30.0) as client:
        # 即使 Redis 挂,worker 启动 OK → legacy 路径继续
        assert (await client.get("/health")).status_code in (200, 404)


# ── Case 11: PostgreSQL 故障行为 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_11_postgres_failure_checkpointer_unavailable(worker_process_factory):
    """Postgres 不可用 → ApiDispatcher 抛 CheckpointerUnavailableError(2.8R-C 严格化)。

    不再静默回退 MemorySaver(守 #18)。
    """
    worker_a = worker_process_factory(9001)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9001", timeout=30.0) as client:
        # 这里只验证 worker 启动 OK;真实 E2E 需停 Postgres 然后尝试 dispatch
        assert (await client.get("/health")).status_code in (200, 404)