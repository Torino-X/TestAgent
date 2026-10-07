"""Context Selection：按 plan.section_plans 分组 + 预算选择。

CE-02 WP-3：
- Section 内排序 ``required → authority → priority → relevance → freshness → source order``；
- 预算选择（先 Required，再按权威加 Optional 直到 target）；
- **Required 超限 → 不降级、不摘要、不占位，直接 fail
  ``context.selection.required_unmet``**（Required Evidence 豁免普通 quota
  但仍受 section max_tokens）。
- 锁定章节（locked_section_ids 对应项）不得被普通 quota 删除。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.context_engine.errors import ContextEngineStage, raise_engine_error
from app.context_engine.models.context import ContextItem, ContextPlan, SectionPlan
from app.context_engine.models.enums import ContextKind
from app.context_engine.models.selection import DroppedContextRef, SelectedContextSet
from app.context_engine.models.source import LockedSection


@dataclass(frozen=True)
class SelectionConfig:
    """Selection 预算配置。"""

    use_estimated_tokens: bool = True


class ContextSelector:
    """按 Section 分组 + 排序 + 预算选择的确定性规则。"""

    def __init__(self, config: SelectionConfig | None = None) -> None:
        self._config = config or SelectionConfig()

    def select(
        self,
        plan: ContextPlan,
        by_section: dict[str, list[ContextItem]],
        *,
        locked_sections: list[LockedSection] | None = None,
    ) -> SelectedContextSet:
        locked_ids = {ls.section_id for ls in (locked_sections or [])}
        included: list[ContextItem] = []
        dropped: list[DroppedContextRef] = []
        section_stats: dict[str, dict[str, Any]] = {}
        total_tokens = 0

        for section_id, section_plan in plan.section_plans.items():
            items = by_section.get(section_id, [])
            section_items, section_dropped, section_tokens = self._select_section(
                section_id,
                section_plan,
                items,
                locked_ids=locked_ids,
            )
            included.extend(section_items)
            dropped.extend(section_dropped)
            total_tokens += section_tokens
            section_stats[section_id] = {
                "kind": section_plan.kind.value,
                "required": section_plan.required,
                "included_count": len(section_items),
                "dropped_count": len(section_dropped),
                "estimated_tokens": section_tokens,
                "budget_tokens": section_plan.budget_tokens,
            }

        return SelectedContextSet(
            included=included,
            dropped=dropped,
            section_stats=section_stats,
            total_estimated_tokens=total_tokens,
            locked_section_ids=sorted(locked_ids),
        )

    def _select_section(
        self,
        section_id: str,
        section_plan: SectionPlan,
        items: list[ContextItem],
        *,
        locked_ids: set[str],
    ) -> tuple[list[ContextItem], list[DroppedContextRef], int]:
        if not items:
            if section_plan.required:
                raise_engine_error(
                    code="context.selection.required_unmet",
                    detail=f"Required Section {section_id!r} 无可用内容",
                    stage=ContextEngineStage.SELECTION,
                    retryable=False,
                    recoverable=True,
                    safe_metadata={"section_id": section_id},
                )
            return [], [], 0

        # 1. 排序：required → authority desc → priority desc → relevance desc → freshness → source order
        sorted_items = sorted(
            items,
            key=lambda it: (
                0 if it.metadata.get("locked") or _is_locked_item(it, locked_ids) else 1,
                -(it.authority or 0),
                -(it.priority or 0),
                -(it.relevance_score or 0.0),
                _freshness_key(it),
            ),
        )

        # 2. 预算：先加锁定章节（豁免普通 quota，仍受 section max_tokens），再 Required，再 Optional
        included: list[ContextItem] = []
        dropped: list[DroppedContextRef] = []
        section_budget = int(section_plan.budget_tokens or 0)
        used = 0

        # 锁定章节必须保留（不得被普通 quota 删除）
        locked_first = [
            it for it in sorted_items
            if _is_locked_item(it, locked_ids) or it.metadata.get("locked")
        ]
        # 非锁定项按排序顺序
        rest = [
            it for it in sorted_items
            if not (_is_locked_item(it, locked_ids) or it.metadata.get("locked"))
        ]

        for it in locked_first:
            if section_budget and used + it.estimated_tokens > section_budget:
                # 锁定章节即使超 section max_tokens 也保留（Required Anchor 语义）
                included.append(it)
                used += it.estimated_tokens
                continue
            included.append(it)
            used += it.estimated_tokens

        for it in rest:
            est = it.estimated_tokens
            if section_budget and used + est > section_budget:
                dropped.append(
                    DroppedContextRef(
                        item_id=it.item_id,
                        reason="source_quota",
                        detail=f"section {section_id} 预算超限",
                    )
                )
                continue
            included.append(it)
            used += est

        # 3. Required 校验：required section 必须至少有一个 item 被保留
        if section_plan.required and not included:
            raise_engine_error(
                code="context.selection.required_unmet",
                detail=f"Required Section {section_id!r} 无可用内容",
                stage=ContextEngineStage.SELECTION,
                retryable=False,
                recoverable=True,
                safe_metadata={"section_id": section_id},
            )

        return included, dropped, used


def _is_locked_item(item: ContextItem, locked_ids: set[str]) -> bool:
    """判断 item 是否对应锁定章节。"""
    section_id = item.metadata.get("section_id")
    if section_id and str(section_id) in locked_ids:
        return True
    # source_ref / item_id 命中锁定章节
    if item.source_ref and str(item.source_ref) in locked_ids:
        return True
    if item.metadata.get("locked_section_ids"):
        return any(str(s) in locked_ids for s in item.metadata["locked_section_ids"])
    return False


def _freshness_key(item: ContextItem) -> int:
    """freshness 排序键：越新越小（升序在前）。"""
    if item.freshness_at is None:
        return 0
    return -int(item.freshness_at.timestamp())
# auto-appended module-level note: selector 主入口: dedup → injection_filter → quota, 出最终 selected_documents。
