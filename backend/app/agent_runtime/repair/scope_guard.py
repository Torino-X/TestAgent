"""Repair Scope Guard (Phase 2.4 — ADR-2.4-13).

Force ``RepairDecision.target_section_ids`` and ``target_issue_ids`` to satisfy:

* ``target_issue_ids ⊆ review_issues.{issue_id}``
* ``target_section_ids ⊆ review_issues.{section_id | global}`` (即与问题相关的 sections)
* ``target_section_ids ∩ locked_section_ids = ∅``
* ``len(target_section_ids) ≤ MAX_REPAIR_SCOPE_SECTIONS`` (default 3)
* Banned tools (``WordExportTool`` 等) 永不调用
"""

from __future__ import annotations

from typing import FrozenSet, Iterable, List, Set

from app.agent_runtime.graphs.test_plan.constants import MAX_REPAIR_SCOPE_SECTIONS
from app.agent_runtime.repair.schemas import RepairDecision, ReviewIssue


class ScopeGuardViolation(ValueError):
    """RepairDecision 违反 scope_guard 限制。"""


BANNED_TOOLS_IN_REPAIR: FrozenSet[str] = frozenset({
    "WordExportTool",
    "TestPlanGeneratorTool",
    "DocxFormatCheckTool",
    "SectionSuggestionTool",
    "RequirementParserTool",
    "TemplateParserTool",
    "UserConfigUpdateTool",
    "ArtifactWriteTool",
    "FileSystemWriteTool",
})


def _derive_reviewed_section_set(review_issues: Iterable[ReviewIssue]) -> Set[str]:
    """从 review_issues 派生"被 review 涉及"的 section 集合。

    对于没有 section_id 的 issue (global),会用一个特殊 sentinel
    ``"__global__"`` 加入集合,但 ``enforce_minimal_scope`` 在
    target_section_ids 中允许 ``target_section_ids=[]`` (即 finish)
    或 ``target_section_ids=["__global__"]`` (代表无章节归属)。
    """
    sections: Set[str] = set()
    for issue in review_issues:
        if issue.section_id:
            sections.add(issue.section_id)
    return sections


def enforce_minimal_scope(
    decision: RepairDecision,
    *,
    review_issues: List[ReviewIssue],
    locked_section_ids: Iterable[str] = (),
    max_scope_sections: int = MAX_REPAIR_SCOPE_SECTIONS,
) -> RepairDecision:
    """Verify that decision does not exceed scope; raise ScopeGuardViolation.

    Returns the same decision instance (no mutation). Caller is expected
    to either ``decision`` is OK or handle ``ScopeGuardViolation`` itself.
    """
    # 1. Banned tools
    if decision.action == "call_tool" and decision.tool_name:
        if decision.tool_name in BANNED_TOOLS_IN_REPAIR:
            raise ScopeGuardViolation(
                f"banned tool {decision.tool_name!r} in Repair Agent; "
                f"never allowed"
            )

    # finish / fail 不校验 target_*_ids (但仍校验 banned tools)
    if decision.action != "call_tool":
        return decision

    target_sections = set(decision.target_section_ids or [])
    target_issues = set(decision.target_issue_ids or [])
    known_issues = {i.issue_id for i in review_issues}
    known_sections = _derive_reviewed_section_set(review_issues)
    locked = set(locked_section_ids or ())

    # 2. 凭空捏造 issue_id
    if unknown := (target_issues - known_issues):
        # empty 也允许(信息收集),但必须在已知集合内
        raise ScopeGuardViolation(
            f"target_issue_ids contains unknown issues: {sorted(unknown)}; "
            f"known={sorted(known_issues)}"
        )

    # 3. target_section_ids 必须 ⊆ review 涉及的 sections
    if unknown := (target_sections - known_sections):
        raise ScopeGuardViolation(
            f"target_section_ids contains unreviewed sections: "
            f"{sorted(unknown)}; reviewed={sorted(known_sections)}"
        )

    # 4. 不允许触碰 locked sections
    if hit := (target_sections & locked):
        raise ScopeGuardViolation(
            f"target_section_ids intersects locked_section_ids: {sorted(hit)}"
        )

    # 5. 长度上限
    if len(target_sections) > max_scope_sections:
        raise ScopeGuardViolation(
            f"target_section_ids length {len(target_sections)} > "
            f"max_scope_sections={max_scope_sections}"
        )

    return decision


__all__ = [
    "ScopeGuardViolation",
    "BANNED_TOOLS_IN_REPAIR",
    "enforce_minimal_scope",
]


# module-level note (auto-appended):
# scope_guard.TaskScopeGuard — 锁住任务内不越界修改。
# 关键约束: 不让任意外部工具越界修改 locked_section_ids。
