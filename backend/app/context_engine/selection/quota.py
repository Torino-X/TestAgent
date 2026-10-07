"""Source Quota Policy：每 source_type 上限。

CE-02 WP-3：
- Required Evidence 豁免普通 quota；
- **锁定章节（locked_section_ids 对应项）不得被普通 quota 删除**
  （豁免 source quota，仍受 section max_tokens）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.context_engine.models.context import ContextItem
from app.context_engine.models.selection import DroppedContextRef
from app.context_engine.models.source import LockedSection


@dataclass(frozen=True)
class SourceQuotaPolicy:
    """每 source_type 的配额上限。"""

    per_source_type_limit: dict[str, int] = field(default_factory=dict)
    default_limit: int = 50

    def limit_for(self, source_type: str) -> int:
        return self.per_source_type_limit.get(source_type, self.default_limit)


class SourceQuotaEnforcer:
    """超配项 → DroppedContextRef(reason=source_quota)。"""

    def __init__(self, policy: SourceQuotaPolicy) -> None:
        self._policy = policy

    def enforce(
        self,
        items: list[ContextItem],
        *,
        locked_ids: set[str],
        required_kinds: set[str] | None = None,
        exempt_source_types: set[str] | None = None,
    ) -> tuple[list[ContextItem], list[DroppedContextRef]]:
        """按 source_type 配额过滤。锁定章节与 Required Evidence 豁免。"""
        included: list[ContextItem] = []
        dropped: list[DroppedContextRef] = []
        counts: dict[str, int] = {}
        exempt_source_types = exempt_source_types or set()

        for it in items:
            source_type = it.source_type.value if hasattr(it.source_type, "value") else str(it.source_type)
            is_locked = it.metadata.get("locked") or _is_locked_item(it, locked_ids)
            is_required_evidence = (
                required_kinds is not None
                and it.kind.value in required_kinds
            )

            if is_locked or is_required_evidence or source_type in exempt_source_types:
                # 豁免普通 quota（仍受 section max_tokens，由 selector 处理）
                included.append(it)
                continue

            limit = self._policy.limit_for(source_type)
            count = counts.get(source_type, 0)
            if count >= limit:
                dropped.append(
                    DroppedContextRef(
                        item_id=it.item_id,
                        reason="source_quota",
                        detail=f"source_type {source_type} 超配额 {limit}",
                    )
                )
                continue
            counts[source_type] = count + 1
            included.append(it)

        return included, dropped


def _is_locked_item(item: ContextItem, locked_ids: set[str]) -> bool:
    section_id = item.metadata.get("section_id")
    if section_id and str(section_id) in locked_ids:
        return True
    if item.source_ref and str(item.source_ref) in locked_ids:
        return True
    if item.metadata.get("locked_section_ids"):
        return any(str(s) in locked_ids for s in item.metadata["locked_section_ids"])
    return False
# auto-appended module-level note: quota: token budget 配额(不让 retrieve 跑超)。
