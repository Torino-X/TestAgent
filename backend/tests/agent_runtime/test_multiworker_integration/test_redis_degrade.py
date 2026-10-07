"""Phase 2.6 — Multi-Worker 集成测试 3/3.

**目标**:验证 Redis 不可达时 LiveEventBusProbe 自动退化为 InMemoryLiveEventBus,
且事件仍能在 worker 间流转(降级路径)。

设计:
- `LiveEventBusProbe.resolve_with_health_check(redis_url=None)` → 同步走 InMemory
- 给一个**格式错误的 Redis URL**(故意连通失败)→ `resolve_with_health_check` 应捕获并退化为 InMemory
- 退化后事件不丢:`publish → subscribe → 收到`,正常 fan-out
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_redis_disconnect_degrades_to_in_memory() -> None:
    """故意给个连不通的 Redis URL → probe 应退化为 InMemory;事件仍可流。"""

    from app.agent_runtime.events.live_event_bus import (
        InMemoryLiveEventBus,
        LiveEventBusProbe,
        RedisLiveEventBus,
    )

    # 给个绝不可能通的端口 + 极短超时(不让 CI hang)
    bad_url = "redis://127.0.0.1:1/0"

    bus = await LiveEventBusProbe.resolve_with_health_check(bad_url)

    # 应该退化到 InMemory(Redis 装/不装都不影响)
    assert isinstance(bus, InMemoryLiveEventBus), (
        f"expected InMemoryLiveEventBus after probe failure; got {type(bus).__name__}"
    )
    assert not isinstance(bus, RedisLiveEventBus)

    # health 必须 True(InMemory 总健康)
    assert await bus.health() is True

    # 退化后事件仍能流
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    await bus.subscribe(task_id="task-degrade", queue=q)

    await bus.publish(
        task_id="task-degrade",
        event={"event_id": "evt-deg-1", "event_type": "plan_step_completed"},
    )

    received = await asyncio.wait_for(q.get(), timeout=1.0)
    assert received["event_id"] == "evt-deg-1"
    assert received["event_type"] == "plan_step_completed"

    await bus.unsubscribe(task_id="task-degrade", queue=q)
    await bus.aclose()


@pytest.mark.asyncio
async def test_no_redis_url_uses_in_memory_directly() -> None:
    """redis_url=None(生产默认)→ InMemoryLiveEventBus 直接使用,不报错。"""

    from app.agent_runtime.events.live_event_bus import (
        InMemoryLiveEventBus,
        LiveEventBusProbe,
    )

    bus = LiveEventBusProbe.resolve(None)
    assert isinstance(bus, InMemoryLiveEventBus)
    assert await bus.health() is True

    # 多个订阅者 fan-out
    q1: asyncio.Queue = asyncio.Queue(maxsize=50)
    q2: asyncio.Queue = asyncio.Queue(maxsize=50)
    await bus.subscribe(task_id="task-fanout", queue=q1)
    await bus.subscribe(task_id="task-fanout", queue=q2)

    await bus.publish(
        task_id="task-fanout",
        event={"event_id": "evt-fanout-1", "event_type": "tool_finished"},
    )

    r1 = await asyncio.wait_for(q1.get(), timeout=1.0)
    r2 = await asyncio.wait_for(q2.get(), timeout=1.0)
    assert r1["event_id"] == "evt-fanout-1"
    assert r2["event_id"] == "evt-fanout-1"

    await bus.unsubscribe(task_id="task-fanout", queue=q1)
    await bus.unsubscribe(task_id="task-fanout", queue=q2)
    await bus.aclose()
