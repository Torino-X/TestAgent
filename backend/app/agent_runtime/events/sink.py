"""``AgentEventSink`` 的内存实现,仅供测试使用。"""

from __future__ import annotations

import threading
from typing import Any, Dict, List


class InMemoryEventSink:
    """线程安全的同步事件收集器。

    与 LangGraph ``ainvoke`` 配合时,``emit`` 是 ``async``;但本身不依赖
    asyncio loop,便于在同步测试 fixture 中直接 ``await``。
    """

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    async def emit(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        node_name: str,
        event_type: str,
        title: str,
        content: str,
        payload: Dict[str, Any] | None = None,
        event_id: str | None = None,
        **_: Any,
    ) -> Dict[str, Any]:
        event = {
            "task_id": task_id,
            "graph_run_id": graph_run_id,
            "node_name": node_name,
            "event_type": event_type,
            "title": title,
            "content": content,
            "payload": payload or {},
        }
        # Phase 2.9B.6: 为内存 sink 生成稳定 event_id,使 Live 与测试路径
        # 的 Tool Identity Contract 行为一致(节点可拿到真实终态事件 id)。
        if event_id is not None:
            event["event_id"] = event_id
        else:
            with self._lock:
                event["event_id"] = f"evt-{len(self.events) + 1}"
        with self._lock:
            self.events.append(event)
        return event

    def collect(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.events)

    def clear(self) -> None:
        with self._lock:
            self.events.clear()


class NullEventSink:
    """静默 sink —— 用于生产热路径,但 Phase 2.0 范围内不挂真实生产路径。"""

    async def emit(self, **_: Any) -> Dict[str, Any]:  # pragma: no cover - trivial
        return {}


__all__ = ["InMemoryEventSink", "NullEventSink"]