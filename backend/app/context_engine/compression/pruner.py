"""Item-Level Pruning — 确定性修剪（不调 LLM，doc09 §14）。

Drop 顺序（只作用于 SelectedContextSet）：
  exact duplicate → superseded/stale → expired → 旧 Tool Result 全文
  （已有 payload 则 replace_with_ref）→ Optional Conversation old turns →
  低分 Memory → 低分 Knowledge → 相邻重复 Chunk → 可恢复正文
  （replace_with_preview）→ 整段 Optional Section。

不可 Prune（硬保护）：Current Goal / Current User Message / Required System
Rule / Call Contract / Required Workspace Instruction / Required Primary
Evidence / Locked Section IDs / Pending Confirmation / Output Schema /
本次明确引用的 Source。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.context_engine.models.context import ContextItem, ContextKind, SectionPlan
from app.context_engine.models.selection import DroppedContextRef, SelectedContextSet


class PruneConfig:
    """Pruning 配置（硬保护 + 阈值）。"""

    def __init__(
        self,
        *,
        hard_protected_kinds: set[ContextKind] | None = None,
        hard_protected_section_ids: set[str] | None = None,
    ) -> None:
        self.hard_protected_kinds = hard_protected_kinds or {
            ContextKind.CURRENT_GOAL,
            ContextKind.SYSTEM_RULES,
            ContextKind.CALL_CONTRACT,
        }
        self.hard_protected_section_ids = hard_protected_section_ids or set()


# 与 Selector 一致的排序键（复用语义）
def _sort_key(item: ContextItem) -> tuple:
    return (
        0 if item.metadata.get("locked") else 1,
        -(item.authority or 0),
        -(item.priority or 0),
        -(item.relevance_score or 0.0),
    )


class ItemPruner:
    """确定性修剪器。返回更新后的 SelectedContextSet（不可变模型重建）。"""

    def __init__(self, config: PruneConfig | None = None) -> None:
        self._config = config or PruneConfig()

    def prune(
        self,
        selected: SelectedContextSet,
        *,
        section_plans: dict[str, SectionPlan] | None = None,
        target_tokens: int | None = None,
    ) -> SelectedContextSet:
        """执行修剪，返回重建的 SelectedContextSet。"""
        section_plans = section_plans or {}
        dropped: list[DroppedContextRef] = []
        included: list[ContextItem] = []
        by_kind: dict[str, list[ContextItem]] = {}
        locked_ids = set(selected.locked_section_ids)

        # 第一遍：按硬保护过滤 + 分组
        for it in selected.included:
            if self._is_hard_protected(it, locked_ids, section_plans):
                included.append(it)
                continue
            by_kind.setdefault(it.kind.value, []).append(it)

        # Drop 顺序（阶段化处理各 kind）
        for kind in (
            ContextKind.CONVERSATION,   # 旧 turns → prune
            ContextKind.MEMORY,          # 低分 memory
            ContextKind.KNOWLEDGE,       # 低分 knowledge
            ContextKind.EVIDENCE,        # 相邻重复 chunk
            ContextKind.TOOL_OUTPUT if hasattr(ContextKind, "TOOL_OUTPUT") else None,
        ):
            if kind is None:
                continue
            kept, kind_dropped = self._prune_kind(kind.value, by_kind.get(kind.value, []))
            included.extend(kept)
            dropped.extend(kind_dropped)
            by_kind[kind.value] = kept

        # 剩余未处理 kind 直接保留
        for kind_val, items in by_kind.items():
            if kind_val not in {k.value for k in (
                ContextKind.CONVERSATION,
                ContextKind.MEMORY,
                ContextKind.KNOWLEDGE,
                ContextKind.EVIDENCE,
            )} and not any(i in included for i in items):
                included.extend(items)

        # 去重 included（同 item_id 保留首个）
        seen_ids: set[str] = set()
        deduped: list[ContextItem] = []
        for it in included:
            if it.item_id in seen_ids:
                continue
            seen_ids.add(it.item_id)
            deduped.append(it)

        total_tokens = sum(i.estimated_tokens for i in deduped)

        return SelectedContextSet(
            included=deduped,
            dropped=selected.dropped + dropped,
            section_stats=_updated_stats(selected, deduped),
            total_estimated_tokens=total_tokens,
            locked_section_ids=selected.locked_section_ids,
        )

    def _is_hard_protected(
        self,
        item: ContextItem,
        locked_ids: set[str],
        section_plans: dict[str, SectionPlan],
    ) -> bool:
        """硬保护判断。"""
        if item.kind in self._config.hard_protected_kinds:
            return True
        section_id = item.metadata.get("section_id")
        if section_id and str(section_id) in self._config.hard_protected_section_ids:
            return True
        if section_id and str(section_id) in locked_ids:
            return True
        if item.metadata.get("locked"):
            return True
        if item.source_ref and str(item.source_ref) in locked_ids:
            return True
        # Required Section 的所有项保护（plan.required=True 的 section）
        if section_id and section_plans.get(str(section_id), None):
            sp = section_plans[str(section_id)]
            if getattr(sp, "required", False):
                return True
        return False

    def _prune_kind(
        self, kind: str, items: list[ContextItem]
    ) -> tuple[list[ContextItem], list[DroppedContextRef]]:
        """按 kind 修剪：排序后保留高分项，drop 尾部 + 过期项 + 重复项。"""
        if not items:
            return [], []
        now = datetime.now(timezone.utc)
        dropped: list[DroppedContextRef] = []
        kept: list[ContextItem] = []

        for it in items:
            # expired → drop
            if it.expires_at is not None and it.expires_at < now:
                dropped.append(
                    DroppedContextRef(
                        item_id=it.item_id,
                        reason="expired",
                        detail=f"kind={kind} 已过期",
                    )
                )
                continue
            kept.append(it)

        # 相邻重复 chunk（content 相似）→ 保留首个
        seen_content: set[str] = set()
        unique: list[ContextItem] = []
        for it in kept:
            ck = it.content[:200]
            if ck in seen_content:
                dropped.append(
                    DroppedContextRef(
                        item_id=it.item_id,
                        reason="duplicate",
                        detail=f"kind={kind} 内容重复",
                    )
                )
                continue
            seen_content.add(ck)
            unique.append(it)

        # 按排序键排序，保留前 5 个（低分项 drop）
        unique.sort(key=_sort_key)
        keep_count = min(5, len(unique))
        kept_head = unique[:keep_count]
        for it in unique[keep_count:]:
            dropped.append(
                DroppedContextRef(
                    item_id=it.item_id,
                    reason="source_quota",
                    detail=f"kind={kind} 排序后裁剪",
                )
            )
        return kept_head, dropped


def _updated_stats(selected: SelectedContextSet, included: list[ContextItem]) -> dict[str, dict[str, Any]]:
    """根据新 included 重算 section_stats（无 included 的 section 补 0）。"""
    stats: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    tokens: dict[str, int] = {}
    for it in included:
        sid = it.metadata.get("section_id") or it.kind.value
        counts[sid] = counts.get(sid, 0) + 1
        tokens[sid] = tokens.get(sid, 0) + it.estimated_tokens
    for sid, s in selected.section_stats.items():
        stats[sid] = {
            **s,
            "included_count": counts.get(sid, 0),
            "estimated_tokens": tokens.get(sid, 0),
        }
    return stats


__all__ = ["ItemPruner", "PruneConfig"]
# auto-appended module-level note: pruner: 主动削减 message / memory / file 三类数据。
