"""Public execution update — user-facing narrative for tool lifecycle events.

This dataclass is the wire format that the orchestrator attaches to
``TOOL_FINISHED`` / ``TOOL_FAILED`` / ``RETRYING`` payloads.  The frontend
renders it directly without parsing tool output, satisfying the project
rules:

- no LLM call (built deterministically from real tool result fields)
- no events added to the SSE protocol (the field lives inside the
  existing payload)
- no DB migration (the dict rides on the existing JSON column)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class PublicExecutionUpdate:
    version: int
    kind: str
    level: str
    headline: str
    summary: str
    impact: str
    next_action: str
    details: tuple[str, ...] = ()
    source: str = "template"
    dedupe_key: str = ""
    # Streaming chunk fields (Section 24-ext).  Defaults make a single-frame
    # legacy payload look identical to the v1 contract: ``chunk_index=0``,
    # ``chunk_total=1``, ``chunk_final=True``.  Newer consumers split the
    # same dataclass across multiple SSE frames; older clients ignore these
    # fields and just see the final frame as the complete card.
    chunk_index: int = 0
    chunk_total: int = 1
    chunk_final: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["details"] = list(self.details)
        return data


def public_update_from_dict(data: dict[str, Any] | None) -> PublicExecutionUpdate | None:
    if not isinstance(data, dict):
        return None
    headline = str(data.get("headline") or "").strip()
    if not headline:
        return None
    try:
        details_value = data.get("details") or ()
        if isinstance(details_value, list):
            details_value = tuple(str(item) for item in details_value)
        return PublicExecutionUpdate(
            version=int(data.get("version", 1)),
            kind=str(data.get("kind", "")),
            level=str(data.get("level", "info")),
            headline=headline,
            summary=str(data.get("summary", "")),
            impact=str(data.get("impact", "")),
            next_action=str(data.get("next_action", "")),
            details=details_value,
            source=str(data.get("source", "template")),
            dedupe_key=str(data.get("dedupe_key", "")),
            chunk_index=_safe_int(data.get("chunk_index", 0), default=0),
            chunk_total=max(1, _safe_int(data.get("chunk_total", 1), default=1)),
            chunk_final=bool(data.get("chunk_final", True)),
        )
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any, default: int) -> int:
    """Coerce a value to int; return ``default`` on any failure.

    Used for the chunk streaming fields so a malformed payload never
    crashes history replay.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# 模块定位:Public Execution Update (PEU) — 工具生命周期的"对外叙事"
#
# 当工具 start / progress / finish 时,orchestrator 把 PEU dataclass 推到
# 事件 envelope (前端 user 可见层)。
#
# 字段:tool_call_id / lifecycle_event / progress_pct / label / ...
# 不可暴露:stack_trace / internal_id / api_key
#
# 链路:
#   graph node / orchestrator 构造 PEU
#     → envelope = { data: peu, public_execution_update: peu }
#     → LiveEventBus + DB 持久化
#
# 关键约束:
#   - 字段白名单(避免 LLM 内部字段泄漏);
#   - 不允许 Pydantic 之外的 dict 推 SSE;
#   - 写入路径被 agent_runtime/_shared/public_narrative.py 复用。
