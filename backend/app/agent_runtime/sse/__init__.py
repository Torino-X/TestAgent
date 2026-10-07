"""SSE subpackage — Phase 2.6 Last-Event-ID replay + 多 worker 实时桥接.

本目录只提供组合组件,不强制替换现有 ``agent_tasks.py:event_stream()``;
后者在 Phase 2.6 范围内不动(禁令 #6)。
"""

from .history_drainer import HistoryDrainer, parse_last_event_id

__all__ = ["HistoryDrainer", "parse_last_event_id"]
