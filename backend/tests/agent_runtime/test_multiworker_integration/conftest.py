"""Phase 2.6 — Multi-Worker integration test fixtures.

所有 multi-worker 集成测试默认 ``skip`` —— 只有设置
``AGENT_RUNTIME_MULTIWORKER_INTEGRATION=1`` 时才真跑。

开启条件:
- ``AGENT_RUNTIME_MULTIWORKER_INTEGRATION=1``
- ``AGENT_RUNTIME_REDIS_URL=redis://localhost:6379/0``(可选;
  不设时退化为"两个 InMemoryLiveEventBus 实例 + 共享中介"模拟)

设计目标:
- 真实 worker 进程启动成本高、CI 慢;本 conftest 模拟"两个
  LiveEventBus 实例"的视角。
- ``redis_url=None`` 时用一个"共享订阅总线"做中转:它本质是把
  publish 转给两个 instance 的本进程 queue —— 等价于 Redis pub/sub
  的语义(广播给所有订阅者),但跑在单进程内。
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Iterator

import pytest


MULTIWORKER_INTEGRATION_ENV = "AGENT_RUNTIME_MULTIWORKER_INTEGRATION"


def _multiworker_enabled() -> bool:
    return os.environ.get(MULTIWORKER_INTEGRATION_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


@pytest.fixture(scope="session")
def multiworker_enabled() -> bool:
    return _multiworker_enabled()


@pytest.fixture(autouse=True)
def _skip_when_disabled(multiworker_enabled: bool) -> Iterator[None]:
    if not multiworker_enabled:
        pytest.skip(
            f"Phase 2.6 multi-worker 集成测试需 {MULTIWORKER_INTEGRATION_ENV}=1 才跑"
        )
    yield


@pytest.fixture
def redis_url() -> str | None:
    """返回 env 中的 Redis URL;None 表示走模拟总线."""
    return os.environ.get("AGENT_RUNTIME_REDIS_URL", "").strip() or None


@pytest.fixture
def shared_pubsub_bus():
    """无 Redis 时的"共享 pubsub"模拟。

    把 publish 广播到所有 registered instance 的本进程 queues。
    行为等价 Redis pub/sub 的 fan-out 语义,跨 instance。
    """

    class _SharedBus:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            # instance_id -> dict[task_id, list[Queue]]
            self._instances: dict[int, dict[str, list[asyncio.Queue]]] = {}
            self._next_id = 1

        def register(self) -> int:
            with self._lock:
                wid = self._next_id
                self._next_id += 1
                self._instances[wid] = {}
                return wid

        def subscribe(self, *, worker_id: int, task_id: str, queue: asyncio.Queue) -> None:
            with self._lock:
                self._instances.setdefault(worker_id, {}).setdefault(task_id, []).append(queue)

        def unsubscribe(self, *, worker_id: int, task_id: str, queue: asyncio.Queue) -> None:
            with self._lock:
                lst = self._instances.get(worker_id, {}).get(task_id, [])
                if queue in lst:
                    lst.remove(queue)

        async def publish(self, *, task_id: str, event: dict) -> None:
            # 跨所有 instance 广播(task_id 维度)
            with self._lock:
                queues: list[asyncio.Queue] = []
                for _wid, by_task in self._instances.items():
                    queues.extend(by_task.get(task_id, []))
            for q in queues:
                # 同步跨 asyncio loop 推送;queue 是 thread-safe,内部 lock
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # 模拟 slow consumer drop;不抛
                    pass

    return _SharedBus()