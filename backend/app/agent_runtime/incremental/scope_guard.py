"""Incremental Scope Guard (Phase 2.5).

Phase 2.5 强制约束:

1. ``decision.target_section_ids`` **必须** ⊆ ``scope.allowed_section_ids``
   (即用户 IntentRouter 写入的 ModificationScope.target_section_ids +
   ``allow_extra_sections=True`` 时才允许扩展)。
2. ``decision.target_section_ids`` **不允许**与 ``locked_section_ids`` 相交。
3. ``decision.scope_kind`` 必须回传,与 ``IncrementalIntent.scope.kind`` 对齐;
   LLM 擅自换任务类型 → 拒。
4. ``BANNED_TOOLS_IN_INCREMENTAL`` 在 decision_filter 里复用:任何 banned
   tool 立刻 fail-fast。
"""

from __future__ import annotations

from typing import FrozenSet, List


BANNED_TOOLS_IN_INCREMENTAL: FrozenSet[str] = frozenset({
    # 全量重生属于 Phase 2.1 流程;Incremental 必须只针对已有 artifact 做局部修改
    "TestPlanGeneratorTool",
    # 不允许直接重做模板解析
    "RequirementParserTool",
    "TemplateParserTool",
    # 不允许 SectionSuggestionTool 重新建议章节
    "SectionSuggestionTool",
    # 不允许 UserConfigUpdateTool / ArtifactWriteTool / FileSystemWriteTool
    "UserConfigUpdateTool",
    "ArtifactWriteTool",
    "FileSystemWriteTool",
})


# ── 长度上限(防 LLM 一次修改太多 section) ───────────────────────────────

MAX_INCREMENTAL_TARGET_SECTIONS: int = 8  # 略大于 Repair,因为整段 export
# 重建一份 artifact 可能涉及更多 sections


class ScopeGuardViolation(Exception):
    """Incremental scope 校验失败。"""


def enforce_minimal_scope(
    decision,
    *,
    scope,
    locked_section_ids: List[str],
) -> None:
    """校验 ``IncrementalDecision`` 是否落在 ``ModificationScope`` 范围内。

    Args:
        decision: ``IncrementalDecision`` 实例(LLM 单步输出)
        scope: ``ModificationScope`` 实例(IntentRouter 写入)
        locked_section_ids: 用户已锁定的 section_id 列表

    Raises:
        ScopeGuardViolation: 任一约束违反时
    """
    # 1. scope_kind 必须回传且与 scope.kind 对齐
    if not decision.scope_kind:
        raise ScopeGuardViolation(
            "incremental_decision_missing_scope_kind",
        )
    if decision.scope_kind != scope.kind:
        raise ScopeGuardViolation(
            f"incremental_decision_scope_kind_mismatch "
            f"(expected={scope.kind}, got={decision.scope_kind})",
        )

    # 2. target_section_ids ⊆ allowed
    allowed = set(scope.target_section_ids or [])
    target = set(decision.target_section_ids or [])

    if not scope.allow_extra_sections and target and not target.issubset(allowed):
        extra = target - allowed
        raise ScopeGuardViolation(
            f"incremental_scope_extra_sections_not_allowed: {sorted(extra)}",
        )

    # 3. target ∩ locked = ∅
    locked = set(locked_section_ids or [])
    if target & locked:
        intersect = sorted(target & locked)
        raise ScopeGuardViolation(
            f"incremental_scope_locked_section_touched: {intersect}",
        )

    # 4. 长度上限
    if len(target) > MAX_INCREMENTAL_TARGET_SECTIONS:
        raise ScopeGuardViolation(
            f"incremental_scope_too_many_sections "
            f"({len(target)} > {MAX_INCREMENTAL_TARGET_SECTIONS})",
        )


__all__ = [
    "BANNED_TOOLS_IN_INCREMENTAL",
    "MAX_INCREMENTAL_TARGET_SECTIONS",
    "ScopeGuardViolation",
    "enforce_minimal_scope",
]

# module-level note (auto-appended):
# scope_guard — 增量修改不超出 user 显式要求的 delta。
# 关键约束: 镜像 repair scope_guard(更严,因为是变更)。
