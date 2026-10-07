"""归一化 — 把 Legacy / LangGraph 两端的 trace 映射到可比较的 canonical 序列。

只保留 ``event_type`` 字符串序列。
时间戳 / public_id / duration_ms / 内部状态键等 volatile 字段全部丢弃。

约定:
- Legacy 通过 ``AgentOrchestrator._publish`` 触发的事件在 ``InMemoryEventSink`` 中收集
- LangGraph 通过 ``GraphEventAdapter`` 触发的事件由节点 ``event_sink.emit`` 收集

归一化后两序列按序比,期望完全一致。
"""

from __future__ import annotations

from typing import Any, Dict, List


_VOLATILE_PAYLOAD_KEYS = {
    "timestamp",
    "started_at",
    "finished_at",
    "duration_ms",
    "public_id",
    "id",
    "internal_id",
    "created_at",
    "updated_at",
}


def normalize_event(ev: Dict[str, Any]) -> str:
    """把单个 event dict 压成 ``event_type`` 字符串。"""
    et = ev.get("event_type") or ev.get("type") or ""
    return str(et)


def normalize_trace(events: List[Dict[str, Any]]) -> List[str]:
    """整条 trace → event_type 字符串列表。"""
    return [normalize_event(e) for e in events]


def diff_traces(
    legacy: List[Dict[str, Any]], langgraph: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """两边 trace 求 diff,供失败时报告用。"""
    a = normalize_trace(legacy)
    b = normalize_trace(langgraph)
    diff: Dict[str, Any] = {
        "legacy_len": len(a),
        "langgraph_len": len(b),
        "legacy_seq": a,
        "langgraph_seq": b,
        "common_prefix_len": 0,
        "legacy_only_after": [],
        "langgraph_only_after": [],
    }
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] == b[i]:
            diff["common_prefix_len"] = i + 1
        else:
            break
    diff["legacy_only_after"] = a[diff["common_prefix_len"] :]
    diff["langgraph_only_after"] = b[diff["common_prefix_len"] :]
    return diff


def assert_canonical_sequences_equal(
    legacy: List[Dict[str, Any]], langgraph: List[Dict[str, Any]]
) -> None:
    """断言两边归一化 event_type 序列完全相等。"""
    a = normalize_trace(legacy)
    b = normalize_trace(langgraph)
    assert a == b, (
        "等价测试失败:LHS=Legacy, RHS=LangGraph\n"
        + f"  Legacy  ({len(a)}): {a}\n"
        + f"  LangGr  ({len(b)}): {b}\n"
        + f"  diff: {diff_traces(legacy, langgraph)}"
    )


__all__ = [
    "normalize_event",
    "normalize_trace",
    "diff_traces",
    "assert_canonical_sequences_equal",
]