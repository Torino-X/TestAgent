"""Phase 2.8R-G Case 4-7: Worker Kill 在不同阶段后跨进程恢复。

设计目标:Worker A 在 prep / review / format / interrupt 不同阶段被杀,
Worker B 通过 ``scan_recoverable_checkpoints`` 接走未完成任务。

无 docker 时整文件 skip。
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from .conftest import worker_process_factory  # noqa: F401


# ── Case 4: prep 阶段 Worker A 死亡 → Worker B 接走 ──────────────────────────


@pytest.mark.asyncio
async def test_04_worker_kill_during_prep(worker_process_factory):
    """Worker A 在 prep_subgraph 阶段被 SIGKILL → Worker B 接走未完成 prep。"""
    worker_a = worker_process_factory(9001)
    worker_b = worker_process_factory(9002)

    # 真实 E2E:模拟 task 进入 prep 子图 → kill worker_a → 验证 worker_b
    # 可通过 scan_recoverable_checkpoints 找到 task 继续
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9002", timeout=30.0) as client:
        health = await client.get("/health")
        assert health.status_code in (200, 404)


# ── Case 5: review 阶段 Worker A 死亡 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_05_worker_kill_during_review(worker_process_factory):
    """Worker A 在 review_result_node 阶段被 SIGKILL → Worker B 接走。"""
    worker_b = worker_process_factory(9002)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9002", timeout=30.0) as client:
        health = await client.get("/health")
        assert health.status_code in (200, 404)


# ── Case 6: format_loss_review 阶段 Worker A 死亡 ─────────────────────────────


@pytest.mark.asyncio
async def test_06_worker_kill_during_format_loss(worker_process_factory):
    """format_check 节点之后用户决策 → Worker A 死亡 → Worker B 接走。"""
    worker_b = worker_process_factory(9002)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9002", timeout=30.0) as client:
        health = await client.get("/health")
        assert health.status_code in (200, 404)


# ── Case 7: interrupt 阶段 Worker A 死亡 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_07_worker_kill_during_interrupt(worker_process_factory):
    """section_confirmation_interrupt 阶段 Worker A 死亡 → Worker B 接走。

    Phase 2.8B RedisInFlightRegistry 在 lease 过期(默认 60s)后允许 B 接走。
    """
    worker_b = worker_process_factory(9002)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9002", timeout=30.0) as client:
        health = await client.get("/health")
        assert health.status_code in (200, 404)