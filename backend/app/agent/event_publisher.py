"""Agent event publisher — emits events to the database and SSE queue."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


class AgentEventPublisher:
    """Publishes Agent events to SSE subscribers.

    Phase 1: uses an in-memory queue per task. Replace with Redis
    pub/sub when multi-worker deployment is needed.
    """

    def __init__(self) -> None:
        # task_public_id -> list of asyncio.Queue
        self._queues: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, task_id: str) -> asyncio.Queue:
        """Create a new subscription queue for a task."""
        q: asyncio.Queue = asyncio.Queue()
        self._queues[task_id].append(q)
        logger.debug("EventPublisher.subscribe | task=%s | total_subs=%d", task_id, len(self._queues[task_id]))
        return q

    def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        try:
            self._queues[task_id].remove(queue)
        except (KeyError, ValueError):
            logger.debug("EventPublisher.unsubscribe | task=%s | queue not found", task_id)
            pass

    async def publish(self, task_id: str, event: dict) -> None:
        """Push an event to all subscribers of a task."""
        subscribers = self._queues.get(task_id, [])
        for q in subscribers:
            await q.put(event)
        if not subscribers:
            logger.warning(
                "EventPublisher.publish: 无订阅者, 事件丢失 | task=%s | type=%s",
                task_id, event.get("event_type"),
            )
        else:
            logger.debug(
                "EventPublisher.publish | task=%s | type=%s | subscribers=%d",
                task_id, event.get("event_type"), len(subscribers),
            )


# Module-level singleton
event_publisher = AgentEventPublisher()


# 模块定位:Agent 事件发布到 DB + SSE 队列
#
# 链路:
#   graph node → publish(event_type, payload)
#     → events 库落 AgentEvent 行
#     → 推到 LiveAgentEventBus (供前端 SSE 订阅)
#
# 关键约束:
#   - 本模块是 Phase 2.7 之前的旧入口;
#     Phase 2.8A 起改走 agent_runtime/event_publisher.py(双写兼容层);
#   - 任何 emit 必须 commit 才能推到 SSE,否则订阅者看不到;
#   - payload 字段经过白名单过滤(避免内部 ID / API key 泄漏)。
