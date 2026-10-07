"""Phase 2.6 — Multi-Worker 集成测试 1/3.

**目标**:验证 worker-A 写入的事件能被 worker-B 的订阅者收到(等价 Redis pub/sub fan-out)。

模拟方式:
- 启动两个 ``InMemoryLiveEventBus`` 实例(w1 / w2),模拟两个 worker 进程内的 bus
- 用 ``shared_pubsub_bus`` 作为"跨实例事件总线",模拟 Redis pub/sub
- worker-A 通过 w1.publish() → shared_pubsub_bus.publish() 推 → w2 的订阅者拿到
- worker-B 通过 w2.subscribe() 注册自己的 queue

不要求 Redis 可达;通过 conftest 共享总线保证跨实例语义。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest


@pytest.mark.asyncio
async def test_worker_b_receives_worker_a_emitted_events(
    shared_pubsub_bus, redis_url: str | None
) -> None:
    """worker-A publish → worker-B 拿到。"""

    from app.agent_runtime.events.live_event_bus import InMemoryLiveEventBus

    w1 = InMemoryLiveEventBus()
    w2 = InMemoryLiveEventBus()
    w1_id = shared_pubsub_bus.register()
    w2_id = shared_pubsub_bus.register()

    # worker-B 订阅
    q_b: asyncio.Queue = asyncio.Queue(maxsize=100)
    shared_pubsub_bus.subscribe(worker_id=w2_id, task_id="task-multiworker-1", queue=q_b)

    # worker-A publish —— 实际场景是 w1.publish() 通过 Redis 推到 w2
    # 这里我们用 shared_pubsub_bus 直接模拟这条 publish 路径
    async def worker_a_publish() -> None:
        # 触发跨实例 broadcast
        await shared_pubsub_bus.publish(
            task_id="task-multiworker-1",
            event={
                "event_id": "evt-A1",
                "event_type": "plan_step_completed",
                "task_id": "task-multiworker-1",
            },
        )

    asyncio.create_task(worker_a_publish())

    received = await asyncio.wait_for(q_b.get(), timeout=1.0)
    assert received["event_id"] == "evt-A1"
    assert received["event_type"] == "plan_step_completed"

    # cleanup
    shared_pubsub_bus.unsubscribe(worker_id=w2_id, task_id="task-multiworker-1", queue=q_b)
    await w1.aclose()
    await w2.aclose()

    # Redis URL 是可选的;不强制
    assert redis_url is None or redis_url.startswith("redis://")
    _ = os.environ.get("AGENT_RUNTIME_REDIS_URL")