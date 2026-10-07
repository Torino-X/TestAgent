"""LangGraph 节点事件接收器。

Phase 2.0 唯一测试实现 = ``InMemoryEventSink``。
Phase 2.1 新增 ``GraphEventAdapter``(把节点事件 fan-out 到
``event_publisher.publish`` + DB ``agent_events`` 行)与
``CancellationEventBuffer``(取消短路缓冲,与 Legacy 行为对齐)。
Phase 2.6 新增:
  * ``LiveEventBus`` 协议 + InMemory/Redis 双实现 — 多 Worker 实时事件总线
  * ``SSESlowConsumerGuard`` — SSE 慢消费者保护
  * ``SequenceNumberAllocator`` — 单任务单调 sequence_no 分配
  * ``LiveAgentEventSink`` — 多 Worker 生产型 sink
"""

from __future__ import annotations

from .cancellation_buffer import CancellationEventBuffer
from .graph_event_adapter import GraphEventAdapter
from .live_agent_event_sink import LiveAgentEventSink
from .live_event_bus import (
    InMemoryLiveEventBus,
    LiveEventBusProbe,
    LiveEventBusProtocol,
    RedisLiveEventBus,
)
from .sequence_allocator import (
    SequenceNumberAllocator,
    compute_idempotency_key,
)
from .sink import InMemoryEventSink, NullEventSink
from .slow_consumer_guard import SSESlowConsumerGuard

__all__ = [
    "CancellationEventBuffer",
    "GraphEventAdapter",
    "InMemoryEventSink",
    "NullEventSink",
    # Phase 2.6
    "LiveEventBusProtocol",
    "InMemoryLiveEventBus",
    "RedisLiveEventBus",
    "LiveEventBusProbe",
    "SSESlowConsumerGuard",
    "SequenceNumberAllocator",
    "compute_idempotency_key",
    "LiveAgentEventSink",
]
