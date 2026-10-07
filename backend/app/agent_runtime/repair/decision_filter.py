"""Repair Decision Filter (Phase 2.4).

镜像 ``app.agent_runtime.preparation.tool_filter`` 的 contract:

* 输入:``RepairDecision``(LLM 单步输出)
* 输出:``(clean_decision, blocked_list)`` —— ``clean_decision`` 把 tool_name
  转换成 ``TestPlanRegenTool`` / ``ResultReviewTool`` / ``KnowledgeSearchTool``
  之一并校验 tool_arguments; 不通过 → append 到 ``blocked_list``
* ``ToolPermissionGuard`` 拦截 whitelist miss / 重复 args(ADR-2.4-1 reused
  shared ``ToolPermissionGuard``)
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.agent_runtime._shared.permission import (
    PermanentPermissionDenied,
    ToolPermissionDenied,
)
from app.agent_runtime._shared.to_fail_decision import make_to_fail_decision
from app.agent_runtime._shared.args_signature import args_signature
from app.agent_runtime.repair.schemas import RepairDecision
from app.agent_runtime.repair.scope_guard import (
    BANNED_TOOLS_IN_REPAIR,
    ScopeGuardViolation,
    enforce_minimal_scope,
)


# ── Per-tool Pydantic arg models ──────────────────────────────────────


class ResultReviewArgs(BaseModel):
    """ResultReviewTool 接受的 tool_arguments 形状。"""

    model_config = ConfigDict(extra="forbid")

    test_plan_content: Dict[str, Any] = Field(default_factory=dict)
    review_standard: Dict[str, Any] = Field(default_factory=dict)
    previous_review: Dict[str, Any] = Field(default_factory=dict)


class TestPlanRegenArgs(BaseModel):
    """TestPlanRegenTool 接受的 tool_arguments 形状(Phase 2.4 bug-fix #2)。

    ``generation_config_subset`` 由 ``regenerate_sections_node`` 自动从
    ``state.template_structure.generation_config`` 派生;LLM 无需填写。
    """

    model_config = ConfigDict(extra="forbid")

    section_ids: List[str] = Field(default_factory=list)
    issues: List[Dict[str, Any]] = Field(default_factory=list)
    test_plan_content: Dict[str, Any] = Field(default_factory=dict)
    template_structure: Dict[str, Any] = Field(default_factory=dict)
    generation_config_subset: Dict[str, Any] = Field(default_factory=dict)
    recovery_mode: str | None = None
    bulk_repair: bool = False


class KnowledgeSearchArgs(BaseModel):
    """KnowledgeSearchTool 接受的 tool_arguments 形状。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=240)
    top_k: int | None = Field(default=None, ge=1, le=50)
    labels: List[str] | None = Field(default=None)


_ARG_MODELS = {
    "ResultReviewTool": ResultReviewArgs,
    "TestPlanRegenTool": TestPlanRegenArgs,
    "KnowledgeSearchTool": KnowledgeSearchArgs,
}


# ── Helpers ────────────────────────────────────────────────────────────


def _to_fail_decision(
    decision: RepairDecision, *, reason: str,
) -> RepairDecision:
    """``RepairDecision``-specific factory delegating to shared."""
    return make_to_fail_decision(RepairDecision)(
        decision, reason=reason,
    )


def _issue_aliases(issue: Any) -> set[str]:
    """Return stable aliases that an LLM may use for the same review issue."""

    values = {
        getattr(issue, "issue_id", None),
        getattr(issue, "rule_id", None),
        getattr(issue, "kind", None),
    }
    section_id = getattr(issue, "section_id", None)
    rule_id = getattr(issue, "rule_id", None)
    kind = getattr(issue, "kind", None)
    if section_id and rule_id:
        values.add(f"{rule_id}:{section_id}")
    if section_id and kind:
        values.add(f"{kind}:{section_id}")
    return {str(value) for value in values if value}


def _normalize_repair_targets(
    decision: RepairDecision,
    *,
    review_issues: List[Any],
) -> RepairDecision:
    """Normalize LLM target ids to canonical ReviewIssue ids.

    The model sometimes emits a rule id (for example
    ``no_tables_for_body_18_level_1``) in ``target_issue_ids`` instead of the
    canonical ``issue_id``. That should not trip scope_guard when the target
    section is still inside the current review scope.
    """

    normalized = decision.model_copy(deep=True)
    by_alias: dict[str, Any] = {}
    for issue in review_issues:
        for alias in _issue_aliases(issue):
            by_alias.setdefault(alias, issue)

    requested_issue_ids = [str(v) for v in normalized.target_issue_ids or [] if v]
    resolved_issue_ids: list[str] = []
    unresolved_issue_ids: list[str] = []
    for raw_id in requested_issue_ids:
        issue = by_alias.get(raw_id)
        if issue is None:
            unresolved_issue_ids.append(raw_id)
            continue
        canonical = str(getattr(issue, "issue_id", raw_id))
        if canonical not in resolved_issue_ids:
            resolved_issue_ids.append(canonical)

    target_sections = {str(v) for v in normalized.target_section_ids or [] if v}
    if (
        normalized.tool_name == "TestPlanRegenTool"
        and target_sections
        and (not resolved_issue_ids or unresolved_issue_ids)
    ):
        # Prefer current review state over LLM-invented ids when section scope is
        # already valid. scope_guard will still reject unknown target sections.
        resolved_issue_ids = [
            str(issue.issue_id)
            for issue in review_issues
            if getattr(issue, "section_id", None) in target_sections
        ]
        unresolved_issue_ids = []

    if resolved_issue_ids:
        normalized.target_issue_ids = resolved_issue_ids
    elif requested_issue_ids:
        normalized.target_issue_ids = requested_issue_ids

    if not normalized.target_section_ids and resolved_issue_ids:
        sections: list[str] = []
        wanted = set(resolved_issue_ids)
        for issue in review_issues:
            if issue.issue_id in wanted and issue.section_id and issue.section_id not in sections:
                sections.append(issue.section_id)
        normalized.target_section_ids = sections

    return normalized


def _canonical_issues_for_decision(
    decision: RepairDecision,
    *,
    review_issues: List[Any],
) -> list[dict[str, Any]]:
    wanted_issue_ids = set(decision.target_issue_ids or [])
    wanted_sections = set(decision.target_section_ids or [])
    selected: list[dict[str, Any]] = []
    for issue in review_issues:
        if wanted_issue_ids and issue.issue_id not in wanted_issue_ids:
            continue
        if wanted_sections and issue.section_id not in wanted_sections:
            continue
        selected.append(issue.model_dump() if hasattr(issue, "model_dump") else dict(issue))
    return selected


def _normalize_test_plan_regen_args(
    decision: RepairDecision,
    *,
    review_issues: List[Any],
) -> RepairDecision:
    if decision.action != "call_tool" or decision.tool_name != "TestPlanRegenTool":
        return decision

    normalized = decision.model_copy(deep=True)
    args = dict(normalized.tool_arguments or {})
    if normalized.target_section_ids:
        args["section_ids"] = list(normalized.target_section_ids)
    if canonical_issues := _canonical_issues_for_decision(
        normalized,
        review_issues=review_issues,
    ):
        args["issues"] = canonical_issues
    normalized.tool_arguments = args
    return normalized


# ── Public API ─────────────────────────────────────────────────────────


def filter_repair_decision(
    decision: RepairDecision,
    *,
    guard,
    review_issues: List,
    locked_section_ids: List[str],
) -> Tuple[RepairDecision, List[Dict[str, Any]]]:
    """校验 decision;不通过则转换 action=fail 并 append 到 blocked_list。

    Returns:
        (clean_decision, blocked_list) where ``blocked_list`` 是 entries:
        [{"tool_name": ..., "reason": ...}, ...]
    """
    blocked: List[Dict[str, Any]] = []
    decision = _normalize_repair_targets(decision, review_issues=review_issues)

    # 1. Banned tools 提前转换(避免 guard 重复计数);但仍让 guard.authorize()
    #    看到此次调用,使 fail_fast_after=2 的连续违规能触发 PermanentPermissionDenied。
    if (
        decision.action == "call_tool"
        and decision.tool_name
        and decision.tool_name in BANNED_TOOLS_IN_REPAIR
    ):
        try:
            # 把 banned tool 调用记录到 guard(用其真实 args_signature)
            guard.authorize(
                tool_name=decision.tool_name,
                args_signature=args_signature(decision.tool_arguments or {}),
            )
        except PermanentPermissionDenied:
            # 第 2+ 次连续违规 → 永久拒绝,向上冒泡
            raise
        except ToolPermissionDenied:
            # 第 1 次违规,记录但不让异常冒泡(正常流程返回 action=fail)
            pass
        return (
            _to_fail_decision(
                decision,
                reason=(
                    f"tool {decision.tool_name!r} is banned in Repair Agent; "
                    f"fallback to legacy regen"
                ),
            ),
            [
                {
                    "tool_name": decision.tool_name,
                    "reason": "banned_tool_in_repair",
                }
            ],
        )

    # 2. action 非 call_tool 时跳过 guard,但仍跑 scope_guard
    if decision.action != "call_tool":
        try:
            enforce_minimal_scope(
                decision,
                review_issues=review_issues,
                locked_section_ids=locked_section_ids,
            )
        except ScopeGuardViolation as exc:
            return (
                _to_fail_decision(
                    decision,
                    reason=f"scope_guard_violation:{exc}",
                ),
                [{"reason": "scope_guard_violation", "error": str(exc)}],
            )
        return decision, blocked

    decision = _normalize_test_plan_regen_args(
        decision,
        review_issues=review_issues,
    )

    # 3. call_tool 路径 — guard 拦截 whitelist miss / 重复
    tool_name = decision.tool_name or ""
    args_sig = args_signature(decision.tool_arguments or {})
    try:
        guard.authorize(tool_name=tool_name, args_signature=args_sig)
    except Exception as exc:  # ToolPermissionDenied / PermanentPermissionDenied
        blocked.append(
            {
                "tool_name": tool_name,
                "reason": "permission_denied",
                "error": str(exc),
            }
        )
        # PermanentPermissionDenied → action=fail; 否则也 fail(简单路由)
        return (
            _to_fail_decision(
                decision,
                reason=f"permission_denied:{exc}",
            ),
            blocked,
        )

    # 4. scope_guard
    try:
        max_scope_sections = None
        if (
            tool_name == "TestPlanRegenTool"
            and isinstance(decision.tool_arguments, dict)
            and decision.tool_arguments.get("recovery_mode") == "json_truncated"
        ):
            max_scope_sections = max(3, len(decision.target_section_ids or []))
        enforce_minimal_scope(
            decision,
            review_issues=review_issues,
            locked_section_ids=locked_section_ids,
            **(
                {"max_scope_sections": max_scope_sections}
                if max_scope_sections is not None else {}
            ),
        )
    except ScopeGuardViolation as exc:
        blocked.append(
            {
                "tool_name": tool_name,
                "reason": "scope_guard_violation",
                "error": str(exc),
            }
        )
        return (
            _to_fail_decision(
                decision,
                reason=f"scope_guard_violation:{exc}",
            ),
            blocked,
        )

    # 5. Per-tool args schema 校验
    arg_model = _ARG_MODELS.get(tool_name)
    if arg_model is not None:
        try:
            arg_model.model_validate(decision.tool_arguments or {})
        except Exception as exc:
            blocked.append(
                {
                    "tool_name": tool_name,
                    "reason": "schema_invalid",
                    "error": str(exc),
                }
            )
            return (
                _to_fail_decision(
                    decision,
                    reason=f"schema_invalid:{str(exc)[:200]}",
                ),
                blocked,
            )

    return decision, blocked


__all__ = [
    "ResultReviewArgs",
    "TestPlanRegenArgs",
    "KnowledgeSearchArgs",
    "filter_repair_decision",
]


# module-level note (auto-appended):
# decision_filter — 修复决策过滤。
# 关键约束: 不允许 LLM 决策绕过 scope_guard。
