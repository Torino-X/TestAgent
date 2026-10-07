"""Phase 2.9C deterministic compressor — string-only, no LLM call.

Inputs are sanitised candidate payload + budget. We:

* Strip empty / duplicated items.
* Apply ``allowlist`` when present in context ``allowed_detail_keys``.
* Truncate ``str`` items.
* Flatten nested ``list[dict]`` and ``list[list[dict]]`` representations
  by summarising the count + showing the first user-visible values.
* Map internal terminology to neutral user copy via a tiny deterministic
  dictionary (Phase 2.9C §11.5).
* Clamp to ``NarrativeContentBudget`` again so the result is byte-safe.

This module NEVER calls the LLM. Its only side effects are
in-memory, and failure is non-fatal for the agent (the governance
service falls back to the raw candidate).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.agent_runtime._shared.narrative_governance.schemas import (
    NarrativeContentBudget,
)


_INTERNAL_TERMS = (
    ("checkpoint恢复失败", "执行状态恢复失败"),
    ("checkpoint recovery failed", "执行状态恢复失败"),
    ("route mismatch", "下一步执行计划不一致"),
    ("scope guard rejected", "修改范围超出当前任务边界"),
    ("schema validation failed", "返回结果格式不符合要求"),
    ("LangGraph Command", "下一步执行指令"),
    ("checkpoint namespace", "执行状态命名"),
    ("task_internal_id", "任务标识"),
    ("SQLAlchemy session", "数据库会话"),
    ("Pydantic validation error", "数据校验异常"),
    ("traceback ", "异常信息 "),
)

_TERM_RE = re.compile(
    "|".join(re.escape(src) for src, _ in _INTERNAL_TERMS)
)


def _apply_terminology(text: str) -> str:
    if not text:
        return text
    return _TERM_RE.sub(lambda m: next(dst for src, dst in _INTERNAL_TERMS if src == m.group(0)), text)


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _truncate(text: Any, limit: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 1:
        return cleaned[:limit]
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def _is_allowlisted(key: str, allowlist: Iterable[str]) -> bool:
    key_norm = key.lower().strip()
    for entry in allowlist:
        if not entry:
            continue
        if entry.lower().strip() == key_norm:
            return True
        if entry.lower().strip() in key_norm:
            return True
    return False


def _project_field(field_name: str, value: Any, *, budget: NarrativeContentBudget) -> str:
    if field_name == "headline":
        return _truncate(value, budget.headline_chars)
    if field_name == "summary":
        return _truncate(value, budget.summary_chars)
    if field_name == "impact":
        return _truncate(value, budget.impact_chars)
    if field_name == "next_action":
        return _truncate(value, budget.next_action_chars)
    return _truncate(value, budget.summary_chars)


def _compress_details(
    details: Any,
    *,
    budget: NarrativeContentBudget,
    allowlist: Iterable[str],
) -> list[str]:
    if details is None:
        return []
    if isinstance(details, str):
        return [_truncate(_apply_terminology(details), budget.detail_item_chars)]
    if isinstance(details, dict):
        items = []
        for key, inner in details.items():
            if allowlist and not _is_allowlisted(str(key), allowlist):
                continue
            items.append(f"{key}: {_truncate(_apply_terminology(inner), budget.detail_item_chars - 8)}")
            if len(items) >= budget.detail_items:
                break
        if len(details) > budget.detail_items:
            items.append(f"…其余 {len(details) - budget.detail_items} 项已省略")
        return items[: budget.detail_items]
    if isinstance(details, list):
        items: list[str] = []
        flat_idx = 0
        for item in details:
            if isinstance(item, str):
                items.append(_truncate(_apply_terminology(item), budget.detail_item_chars))
            elif isinstance(item, dict):
                # Show a single readable line per dict entry instead
                # of a repr-like dump.
                preview = ", ".join(
                    f"{k}={_truncate(str(v), 32)}"
                    for k, v in list(item.items())[:3]
                )
                items.append(_truncate(preview, budget.detail_item_chars))
            elif isinstance(item, list):
                sub = [
                    _truncate(_apply_terminology(x), budget.detail_item_chars)
                    if isinstance(x, str) else str(x)
                    for x in item[:5]
                ]
                items.append(_truncate("[" + "; ".join(sub) + "]", budget.detail_item_chars))
            else:
                items.append(_truncate(str(item), budget.detail_item_chars))
            flat_idx += 1
            if flat_idx >= budget.detail_items:
                break
        if len(details) > budget.detail_items:
            items.append(f"…其余 {len(details) - budget.detail_items} 项已省略")
        return items
    return [_truncate(str(details), budget.detail_item_chars)]


def compress(
    candidate: dict[str, Any] | None,
    *,
    budget: NarrativeContentBudget,
    allowlist: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Deterministically compress the public-update candidate.

    Returns a new dict (the input is not mutated).
    """
    if not isinstance(candidate, dict):
        return {}
    allow = list(allowlist or [])

    result: dict[str, Any] = {}
    for field in ("headline", "summary", "impact", "next_action"):
        result[field] = _project_field(field, candidate.get(field, ""), budget=budget)
        result[field] = _apply_terminology(result[field])

    result["details"] = _compress_details(
        candidate.get("details"),
        budget=budget,
        allowlist=allow,
    )
    # Carry level through so the wire payload stays readable.
    if "level" in candidate:
        result["level"] = str(candidate.get("level") or "info")
    if "kind" in candidate:
        result["kind"] = str(candidate.get("kind") or "")
    return result


def measure_chars(payload: dict[str, Any] | None) -> int:
    """Count payload characters for telemetry."""
    if not payload:
        return 0
    detail_chars = 0
    details = payload.get("details") or []
    if isinstance(details, list):
        for item in details:
            detail_chars += len(str(item))
    return (
        len(str(payload.get("headline") or ""))
        + len(str(payload.get("summary") or ""))
        + len(str(payload.get("impact") or ""))
        + len(str(payload.get("next_action") or ""))
        + detail_chars
    )


__all__ = ["compress", "measure_chars"]
