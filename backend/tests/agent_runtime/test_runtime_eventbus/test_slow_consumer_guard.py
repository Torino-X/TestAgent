"""Phase 2.6 — SSESlowConsumerGuard bounds / timeout / validation tests."""

from __future__ import annotations

import asyncio
import pytest

from app.agent_runtime.events.slow_consumer_guard import SSESlowConsumerGuard


def test_guard_rejects_invalid_max_queue() -> None:
    with pytest.raises(ValueError):
        SSESlowConsumerGuard(max_queue=0)
    with pytest.raises(ValueError):
        SSESlowConsumerGuard(max_queue=-1)


def test_guard_rejects_invalid_block_timeout() -> None:
    with pytest.raises(ValueError):
        SSESlowConsumerGuard(block_timeout=0)
    with pytest.raises(ValueError):
        SSESlowConsumerGuard(block_timeout=-0.5)


@pytest.mark.asyncio
async def test_guard_returns_true_on_normal_put() -> None:
    guard = SSESlowConsumerGuard(max_queue=100, block_timeout=1.0)
    q: asyncio.Queue = asyncio.Queue()
    ok = await guard.try_put(q, {"event_type": "x"})
    assert ok is True
    assert q.qsize() == 1


@pytest.mark.asyncio
async def test_guard_returns_false_when_queue_full() -> None:
    guard = SSESlowConsumerGuard(max_queue=3, block_timeout=0.5)
    q: asyncio.Queue = asyncio.Queue()
    # Fill the queue directly to bypass the guard
    for i in range(3):
        q.put_nowait({"n": i})
    # 4th attempt — qsize already at max → guard returns False without blocking
    ok = await guard.try_put(q, {"n": 99})
    assert ok is False
    assert q.qsize() == 3