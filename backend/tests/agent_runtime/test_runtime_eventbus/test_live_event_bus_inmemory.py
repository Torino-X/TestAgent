"""Phase 2.6 — InMemoryLiveEventBus fan-out / unsubscribe / aclose tests."""

from __future__ import annotations

import asyncio
import pytest

from app.agent_runtime.events.live_event_bus import InMemoryLiveEventBus
from app.agent_runtime.events.slow_consumer_guard import SSESlowConsumerGuard


@pytest.mark.asyncio
async def test_inmemory_publish_fan_out_to_all_subscribers() -> None:
    bus = InMemoryLiveEventBus()
    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()
    await bus.subscribe(task_id="t1", queue=q1)
    await bus.subscribe(task_id="t1", queue=q2)

    await bus.publish(task_id="t1", event={"event_type": "x", "n": 1})
    await bus.publish(task_id="t1", event={"event_type": "x", "n": 2})

    assert (await q1.get())["n"] == 1
    assert (await q2.get())["n"] == 1
    assert (await q1.get())["n"] == 2
    assert (await q2.get())["n"] == 2
    assert q1.empty() and q2.empty()
    assert bus.subscriber_count("t1") == 2


@pytest.mark.asyncio
async def test_inmemory_publish_to_unknown_task_is_noop() -> None:
    bus = InMemoryLiveEventBus()
    # Should not raise even though no subscribers exist
    await bus.publish(task_id="missing", event={"event_type": "x"})
    assert bus.subscriber_count("missing") == 0


@pytest.mark.asyncio
async def test_inmemory_unsubscribe_removes_subscriber() -> None:
    bus = InMemoryLiveEventBus()
    q: asyncio.Queue = asyncio.Queue()
    await bus.subscribe(task_id="t1", queue=q)
    await bus.unsubscribe(task_id="t1", queue=q)
    assert bus.subscriber_count("t1") == 0

    # Publishing after unsubscribe — q stays empty
    await bus.publish(task_id="t1", event={"n": 1})
    assert q.empty()


@pytest.mark.asyncio
async def test_inmemory_aclose_drains_subscribers() -> None:
    bus = InMemoryLiveEventBus()
    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()
    await bus.subscribe(task_id="t1", queue=q1)
    await bus.subscribe(task_id="t2", queue=q2)

    await bus.aclose()
    assert bus.subscriber_count("t1") == 0
    assert bus.subscriber_count("t2") == 0


@pytest.mark.asyncio
async def test_inmemory_health_always_true() -> None:
    bus = InMemoryLiveEventBus()
    assert await bus.health() is True


@pytest.mark.asyncio
async def test_inmemory_slow_consumer_is_dropped_on_overflow() -> None:
    """max_queue=2 + block_timeout tiny → 第二条 publish 后,慢消费者被踢。"""
    guard = SSESlowConsumerGuard(max_queue=2, block_timeout=0.05)
    bus = InMemoryLiveEventBus(slow_guard=guard)
    q: asyncio.Queue = asyncio.Queue()
    await bus.subscribe(task_id="t1", queue=q)

    # 灌 3 个事件;第三个 publish 时 qsize=2 >= max_queue → 丢
    await bus.publish(task_id="t1", event={"n": 1})
    await bus.publish(task_id="t1", event={"n": 2})
    await bus.publish(task_id="t1", event={"n": 3})

    # Slow consumer 已 unsubscribed
    assert bus.subscriber_count("t1") == 0