"""Context Selection 结果模型：SelectedContextSet / DroppedContextRef。

CE-02 WP-3：selector 产出被保留与被丢弃的 ContextItem 引用。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.context_engine.models.context import ContextItem, ContextRef


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DroppedContextRef(FrozenModel):
    """被丢弃项的引用与原因（state-safe，不携带内容）。"""

    item_id: str
    reason: Literal[
        "duplicate",
        "expired",
        "scope_filtered",
        "source_quota",
        "source_type_not_allowed",
        "superseded",
        "soft_budget_prune",
        "unsafe_content",
    ]
    detail: str | None = None


class SelectedContextSet(FrozenModel):
    """Selection 输出：保留项 + 丢弃记录 + 分节统计。"""

    included: list[ContextItem] = Field(default_factory=list)
    dropped: list[DroppedContextRef] = Field(default_factory=list)
    section_stats: dict[str, dict[str, Any]] = Field(default_factory=dict)
    total_estimated_tokens: int = Field(default=0, ge=0)
    locked_section_ids: list[str] = Field(default_factory=list)

    def refs(self) -> list[ContextRef]:
        return [
            ContextRef(
                item_id=item.item_id,
                kind=item.kind,
                source_type=item.source_type,
                source_ref=item.source_ref,
            )
            for item in self.included
        ]

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "included_count": len(self.included),
            "dropped_count": len(self.dropped),
            "dropped_reasons": [d.reason for d in self.dropped],
            "section_stats": self.section_stats,
            "total_estimated_tokens": self.total_estimated_tokens,
            "locked_section_ids": self.locked_section_ids,
        }
# auto-appended module-level note: selection 模型: SelectionStage 配额 + injection filter 决策。
