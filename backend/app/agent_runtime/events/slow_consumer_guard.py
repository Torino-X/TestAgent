"""SSE 慢消费者守卫(Phase 2.6)。

当某个 SSE 订阅者跟不上事件流时(网速慢/客户端卡住),``asyncio.Queue.put``
会阻塞整个 Worker。如果不限制,一个慢客户端就能拖垮整个进程。

策略:
  * max_queue=1000(可配):超过即丢,不等阻塞
  * block_timeout=1.0(可配):put 阻塞超过 1s 视为 slow,返回 False
  * caller 收到 False 应当 unsubscribe + 推 ``sse_slow_consumer_disconnected`` 控制帧

不抛异常 — 失败/丢事件属 SSE 协议内可接受行为;事件仍然在 MySQL,客户端
可以 reconnect via Last-Event-ID 接回。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class SSESlowConsumerGuard:
    """SSE subscriber queue 上限保护。"""

    def __init__(self, max_queue: int = 1000, block_timeout: float = 1.0) -> None:
        if max_queue <= 0:
            raise ValueError("max_queue must be > 0")
        if block_timeout <= 0:
            raise ValueError("block_timeout must be > 0")
        self.max_queue = int(max_queue)
        self.block_timeout = float(block_timeout)

    async def try_put(self, queue: asyncio.Queue, event: dict[str, Any]) -> bool:
        """尝试把 event 放入 subscriber queue;返回 True=成功,False=慢消费者。

        优先检查 qsize() — 若已超 max_queue,立即丢,不阻塞。
        否则 await put(block_timeout);超时视为 slow。
        """
        try:
            current = queue.qsize()
        except Exception:  # pragma: no cover - defensive
            current = 0
        if current >= self.max_queue:
            logger.debug(
                "SSESlowConsumerGuard: queue full (size=%d); dropping event", current
            )
            return False
        try:
            await asyncio.wait_for(queue.put(event), timeout=self.block_timeout)
            return True
        except asyncio.TimeoutError:
            logger.debug(
                "SSESlowConsumerGuard: put blocked > %ss; dropping event",
                self.block_timeout,
            )
            return False


__all__ = ["SSESlowConsumerGuard"]
