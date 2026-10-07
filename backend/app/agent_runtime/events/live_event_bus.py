"""Live Event Bus — Phase 2.6 多 Worker 实时事件总线。

设计要点(ADR-2.6-1/2/18):
  * Redis 仅做实时 pub/sub;MySQL 仍是历史 SoT(禁令硬约束)
  * 不可用 → 静默退化 InMemory(单 worker 模式)
  * ``AGENT_RUNTIME_REDIS_URL`` env opt-in 才用 Redis

Public API:
  * ``LiveEventBusProtocol`` — Protocol 契约
  * ``InMemoryLiveEventBus`` — 进程内广播(默认;degraded 模式)
  * ``RedisLiveEventBus`` — 跨进程广播(可选;需 Redis)
  * ``LiveEventBusProbe`` — lifespan 启动探测;Redis 不通 → 退化

订阅模式:
  * subscribe(task_id, queue) → 返回同一个 queue;caller 自己 drive consume
  * publish(task_id, event) → fan-out 到该 task 所有本地 + Redis 其他 worker
  * unsubscribe(task_id, queue) → 移除本地订阅;Redis channel 由 reader task 取消

事件 payload:
  * JSON-safe dict(str/int/float/bool/list/dict/None);无 datetime/Object
  * event_id / sequence_no / idempotency_key 由 LiveAgentEventSink 注入
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import defaultdict
from typing import Any, Protocol, runtime_checkable

from .slow_consumer_guard import SSESlowConsumerGuard

logger = logging.getLogger(__name__)


@runtime_checkable
class LiveEventBusProtocol(Protocol):
    """实时事件总线契约。"""

    async def publish(self, *, task_id: str, event: dict[str, Any]) -> None: ...

    async def subscribe(
        self, *, task_id: str, queue: asyncio.Queue
    ) -> asyncio.Queue: ...

    async def unsubscribe(
        self, *, task_id: str, queue: asyncio.Queue
    ) -> None: ...

    async def health(self) -> bool: ...

    async def aclose(self) -> None: ...


class InMemoryLiveEventBus:
    """进程内广播 — Phase 2.6 默认(degraded + 单 worker 模式)。

    线程安全(``threading.Lock`` 保护订阅表);async 接口本身不需要锁,
    但 subscribe/unsubscribe 可能在不同 loop 调用,所以加锁。

    慢消费者策略:
      * SSESlowConsumerGuard.try_put(queue, event)
      * 满/超时 → 视为 slow,自动 unsubscribe + 日志 WARN
    """

    def __init__(self, slow_guard: SSESlowConsumerGuard | None = None) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._lock = threading.Lock()
        self._slow_consumer = slow_guard or SSESlowConsumerGuard()

    async def publish(self, *, task_id: str, event: dict[str, Any]) -> None:
        # 复制快照避免长持锁;同时容忍 unsubscribe 并发
        with self._lock:
            subs = list(self._subs.get(task_id, []))
        if not subs:
            # 没有本地订阅;在多 worker 模式下,RedisLiveEventBus 会接走;
            # InMemory 单 worker 模式下,订阅者没接是异常但不应阻塞发布
            logger.warning(
                "InMemoryLiveEventBus.publish: 无订阅者 | task_id=%s | event_type=%s",
                task_id, event.get("event_type"),
            )
            return

        dropped: list[asyncio.Queue] = []
        for q in subs:
            ok = await self._slow_consumer.try_put(q, event)
            if not ok:
                dropped.append(q)
        for q in dropped:
            await self.unsubscribe(task_id=task_id, queue=q)
        if dropped:
            logger.warning(
                "InMemoryLiveEventBus: dropped %d slow consumers for task_id=%s",
                len(dropped),
                task_id,
            )

    async def subscribe(
        self, *, task_id: str, queue: asyncio.Queue
    ) -> asyncio.Queue:
        with self._lock:
            self._subs[task_id].append(queue)
        return queue

    async def unsubscribe(self, *, task_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            try:
                self._subs[task_id].remove(queue)
            except ValueError:
                pass
            if not self._subs[task_id]:
                self._subs.pop(task_id, None)

    async def health(self) -> bool:
        return True

    async def aclose(self) -> None:
        with self._lock:
            for task_id, queues in list(self._subs.items()):
                for q in queues:
                    try:
                        # 用 put_nowait 防止阻塞;失败也没关系,queue 即将 GC
                        q.put_nowait({"_type": "sse_closing", "task_id": task_id})
                    except Exception:
                        pass
            self._subs.clear()

    # 测试辅助
    def subscriber_count(self, task_id: str) -> int:
        with self._lock:
            return len(self._subs.get(task_id, []))


class RedisLiveEventBus:
    """跨进程广播 — Phase 2.6 多 worker 生产路径。

    频道命名:``agent_event:bus:<task_id>``。

    读模型:每个 subscriber 启动一个独立的 redis pubsub reader task;cancel
    reader 即 unsubscribe。publish 用独立 client(避免与 pubsub 共享导致
    pipeline 死锁)。

    健康:``health()`` 跑 ``PING``;若返回 False → caller 退化 InMemory。
    """

    PUBSUB_KEY_PREFIX = "agent_event:bus:"

    def __init__(
        self,
        redis_url: str,
        slow_guard: SSESlowConsumerGuard | None = None,
        decode_responses: bool = False,
    ) -> None:
        # 延迟 import:Phase 2.6 默认不强制装 redis
        try:
            import redis.asyncio as aioredis  # type: ignore
        except ImportError as exc:  # pragma: no cover - install-time
            raise RuntimeError(
                "RedisLiveEventBus requires `redis>=5.0.0`. "
                "Install with: pip install 'redis>=5.0.0,<6.0.0'"
            ) from exc

        self._redis_url = redis_url
        self._decode_responses = decode_responses
        self._client = aioredis.from_url(
            redis_url, decode_responses=decode_responses
        )
        self._slow_consumer = slow_guard or SSESlowConsumerGuard()
        self._local_subs: dict[
            str, list[tuple[asyncio.Queue, asyncio.Task]]
        ] = defaultdict(list)
        self._lock = threading.Lock()

    def _channel(self, task_id: str) -> str:
        return f"{self.PUBSUB_KEY_PREFIX}{task_id}"

    async def publish(self, *, task_id: str, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False, default=str).encode("utf-8")
        try:
            await self._client.publish(self._channel(task_id), payload)
        except Exception:
            # Redis 故障不抛 — 历史在 MySQL,客户端可走 Last-Event-ID
            logger.warning(
                "RedisLiveEventBus: publish failed task_id=%s; event still in MySQL",
                task_id,
                exc_info=True,
            )

    async def subscribe(
        self, *, task_id: str, queue: asyncio.Queue
    ) -> asyncio.Queue:
        ps = self._client.pubsub()
        try:
            await ps.subscribe(self._channel(task_id))
        except Exception:
            logger.warning(
                "RedisLiveEventBus: subscribe failed task_id=%s", task_id, exc_info=True
            )
            try:
                await ps.aclose()
            except Exception:
                pass
            return queue

        async def reader() -> None:
            try:
                async for msg in ps.listen():
                    if msg.get("type") != "message":
                        continue
                    data = msg.get("data")
                    try:
                        if isinstance(data, (bytes, bytearray)):
                            text = data.decode("utf-8")
                            ev = json.loads(text)
                        elif isinstance(data, str):
                            ev = json.loads(data)
                        else:
                            continue
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        logger.warning(
                            "RedisLiveEventBus: bad payload on channel task_id=%s", task_id
                        )
                        continue
                    await self._slow_consumer.try_put(queue, ev)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "RedisLiveEventBus: reader crashed task_id=%s", task_id, exc_info=True
                )
            finally:
                try:
                    await ps.unsubscribe(self._channel(task_id))
                except Exception:
                    pass
                try:
                    await ps.aclose()
                except Exception:
                    pass

        task = asyncio.create_task(reader(), name=f"live-bus-reader:{task_id}")
        with self._lock:
            self._local_subs[task_id].append((queue, task))
        return queue

    async def unsubscribe(self, *, task_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            entries = self._local_subs.get(task_id, [])
            keep: list[tuple[asyncio.Queue, asyncio.Task]] = []
            for q, t in entries:
                if q is queue:
                    t.cancel()
                else:
                    keep.append((q, t))
            if keep:
                self._local_subs[task_id] = keep
            else:
                self._local_subs.pop(task_id, None)

    async def health(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def aclose(self) -> None:
        with self._lock:
            all_tasks: list[asyncio.Task] = [
                t for entries in self._local_subs.values() for _, t in entries
            ]
            self._local_subs.clear()
        for t in all_tasks:
            t.cancel()
        # 给 reader task 一点时间退出
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        try:
            await self._client.aclose()
        except Exception:
            pass


class LiveEventBusProbe:
    """lifespan 启动探测 — 选择正确的 bus 实现并自动退化。"""

    @classmethod
    def resolve(
        cls,
        redis_url: str | None,
        *,
        slow_guard: SSESlowConsumerGuard | None = None,
    ) -> LiveEventBusProtocol:
        """同步入口(lifespan 调用前);不进行实际 health check。

        Use ``resolve_with_health_check`` 在异步 lifespan 中跑 PING。
        """
        if not redis_url:
            logger.info(
                "LiveEventBusProbe: no AGENT_RUNTIME_REDIS_URL; using InMemoryLiveEventBus"
            )
            return InMemoryLiveEventBus(slow_guard=slow_guard)
        try:
            bus: LiveEventBusProtocol = RedisLiveEventBus(
                redis_url, slow_guard=slow_guard
            )
            logger.info(
                "LiveEventBusProbe: RedisLiveEventBus initialized for %s", redis_url
            )
            return bus
        except RuntimeError as exc:
            logger.warning(
                "LiveEventBusProbe: RedisLiveEventBus init failed (%s); degraded to InMemory",
                exc,
            )
            return InMemoryLiveEventBus(slow_guard=slow_guard)

    @classmethod
    async def resolve_with_health_check(
        cls,
        redis_url: str | None,
        *,
        slow_guard: SSESlowConsumerGuard | None = None,
        health_timeout_seconds: float = 2.0,
    ) -> LiveEventBusProtocol:
        """异步入口:实际跑 PING 验证 Redis 可用;失败退化。"""
        if not redis_url:
            logger.info(
                "LiveEventBusProbe: no AGENT_RUNTIME_REDIS_URL; using InMemoryLiveEventBus"
            )
            return InMemoryLiveEventBus(slow_guard=slow_guard)
        try:
            bus = RedisLiveEventBus(redis_url, slow_guard=slow_guard)
            # This method is also called directly by FastAPI lifespan after
            # the startup probe.  Bound that second Redis PING as well: an
            # unreachable endpoint must degrade to InMemory, not block bind.
            ok = await asyncio.wait_for(
                bus.health(), timeout=health_timeout_seconds
            )
            if ok:
                logger.info(
                    "LiveEventBusProbe: RedisLiveEventBus ready at %s", redis_url
                )
                return bus
            logger.warning(
                "LiveEventBusProbe: Redis ping returned False; degraded to InMemory"
            )
            await bus.aclose()
            return InMemoryLiveEventBus(slow_guard=slow_guard)
        except Exception as exc:
            logger.warning(
                "LiveEventBusProbe: RedisLiveEventBus unavailable (%s); degraded to InMemory",
                exc,
            )
            return InMemoryLiveEventBus(slow_guard=slow_guard)


__all__ = [
    "LiveEventBusProtocol",
    "InMemoryLiveEventBus",
    "RedisLiveEventBus",
    "LiveEventBusProbe",
    "SSESlowConsumerGuard",
]
