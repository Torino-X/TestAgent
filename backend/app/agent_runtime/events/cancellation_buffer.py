"""CancellationEventBuffer — Phase 2.1 AgentEventSink 取消短路缓冲。

当 ``cancellation_service.is_cancelled(task_id)`` 为 True 时,
GraphEventAdapter 把事件 append 到本缓冲,不再走 DB / SSE,避免取消后
还能惊扰订阅者(与 Legacy event_publisher 短路行为一致)。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List


class CancellationEventBuffer:
    """任务级事件缓冲 + 已丢弃计数。"""

    def __init__(self) -> None:
        self._events: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._dropped_count: Dict[str, int] = defaultdict(int)

    def append(self, event: Dict[str, Any]) -> None:
        task_id = str(event.get("task_id") or "")
        if not task_id:
            return
        self._events[task_id].append(event)
        self._dropped_count[task_id] += 1

    def drain(self, task_id: str) -> List[Dict[str, Any]]:
        events = self._events.get(str(task_id), [])
        self._events[str(task_id)] = []
        return list(events)

    def dropped_count(self, task_id: str) -> int:
        return int(self._dropped_count.get(str(task_id), 0))

    def clear(self, task_id: str | None = None) -> None:
        if task_id is None:
            self._events.clear()
            self._dropped_count.clear()
        else:
            self._events.pop(str(task_id), None)
            self._dropped_count.pop(str(task_id), None)


__all__ = ["CancellationEventBuffer"]
