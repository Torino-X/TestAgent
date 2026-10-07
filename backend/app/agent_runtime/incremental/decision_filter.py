"""Incremental Decision Filter (Phase 2.5).

镜像 ``preparation.tool_filter`` / ``repair.decision_filter`` 的设计,但多一层
``enforce_minimal_scope``(由 ``scope_guard.py`` 提供)。

四层校验:

1. **白名单**(rule 12): ``decision.tool_name`` 必须在
   ``INCREMENTAL_TOOL_WHITELIST`` 内,否则 fail-fast;
2. **banned list**:任何 ``BANNED_TOOLS_IN_INCREMENTAL`` 命中 → action=fail,
   且计 permanent denial(防 LLM 反复请求);
3. **Schema 校验**(rule 13): 每个允许的工具参数走 Pydantic 模型;
4. **Scope Guard**: 详见 ``scope_guard.enforce_minimal_scope``。

返回 ``(clean_decision, blocked_records)`` —— clean 是修复后的决策,
blocked 是拦截审计记录(入 ``repair_steps`` 风格审计链)。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

from pydantic import BaseModel, Field

from app.agent_runtime._shared.permission import (
    PermanentPermissionDenied,
    ToolPermissionDenied,
    ToolPermissionGuard,
)
from app.agent_runtime._shared.to_fail_decision import make_to_fail_decision
from app.agent_runtime.incremental.scope_guard import (
    BANNED_TOOLS_IN_INCREMENTAL,
    ScopeGuardViolation,
    enforce_minimal_scope,
)


# ── Per-tool Pydantic arg schemas (rule 13) ───────────────────────


class TestPlanRegenArgs(BaseModel):
    """``TestPlanRegenTool`` 入参(Phase 2.5 增量场景)。"""

    model_config = {"extra": "forbid"}

    section_ids: List[str] = Field(min_length=1, max_length=8)
    issues: List[Dict[str, Any]] = Field(default_factory=list, max_length=16)
    generation_config_subset: Dict[str, Any] = Field(default_factory=dict)


class ResultReviewArgs(BaseModel):
    """``ResultReviewTool`` 入参(Phase 2.5 增量后的局部重审)。"""

    model_config = {"extra": "forbid"}

    target_section_ids: List[str] = Field(default_factory=list, max_length=8)
    review_standard: Dict[str, Any] = Field(default_factory=dict)


class KnowledgeSearchArgs(BaseModel):
    """``KnowledgeSearchTool`` 入参。"""

    model_config = {"extra": "forbid"}

    query: str = Field(min_length=1, max_length=200)
    top_k: int = Field(default=5, ge=1, le=20)
    labels: List[str] = Field(default_factory=list, max_length=10)


class WordExportArgs(BaseModel):
    """``WordExportTool`` 入参(增量版本化导出)。"""

    model_config = {"extra": "forbid"}

    artifact_public_id: str = Field(min_length=1, max_length=64)
    version_no: int = Field(ge=2, le=999)  # 增量一定是 version_no >= 2
    template_file_id: str = Field(min_length=1, max_length=64)


class DocxFormatCheckArgs(BaseModel):
    """``DocxFormatCheckTool`` 入参(增量后局部格式检查)。"""

    model_config = {"extra": "forbid"}

    artifact_public_id: str = Field(min_length=1, max_length=64)
    target_section_ids: List[str] = Field(default_factory=list, max_length=8)


# ── Decision filter ─────────────────────────────────────────────


# Per-tool Pydantic schema 映射
_INCREMENTAL_TOOL_ARG_SCHEMAS: Dict[str, type[BaseModel]] = {
    "TestPlanRegenTool": TestPlanRegenArgs,
    "ResultReviewTool": ResultReviewArgs,
    "KnowledgeSearchTool": KnowledgeSearchArgs,
    "WordExportTool": WordExportArgs,
    "DocxFormatCheckTool": DocxFormatCheckArgs,
}


def filter_incremental_decision(
    decision,
    *,
    guard: ToolPermissionGuard,
    scope,
    locked_section_ids: List[str],
    whitelist: frozenset,
    state: Dict[str, Any] | None = None,
) -> Tuple:
    """校验 ``IncrementalDecision`` 落入允许范围。

    Args:
        decision: ``IncrementalDecision`` 实例
        guard: 共享 ``ToolPermissionGuard``(Phase 2.5 复用 _shared)
        scope: ``ModificationScope`` 实例(IntentRouter 写入)
        locked_section_ids: 用户已锁定的 section_id 列表
        whitelist: ``INCREMENTAL_TOOL_WHITELIST`` 或测试用子集

    Returns:
        ``(clean_decision, blocked_records)``: clean 可能被转换为 action=fail;
        blocked_records 是审计列表
    """
    blocked: List[Dict[str, Any]] = []
    fail_factory = make_to_fail_decision(type(decision))

    # 1. action=call_tool 才需要校验工具
    if decision.action != "call_tool":
        return decision, blocked

    tool_name = decision.tool_name or ""

    # 2. banned list 优先拦截
    if tool_name in BANNED_TOOLS_IN_INCREMENTAL:
        blocked.append({
            "outcome": "banned",
            "tool_name": tool_name,
            "reason": f"tool_banned:{tool_name}",
        })
        return fail_factory(decision, reason=f"tool_banned:{tool_name}"), blocked

    # 3. whitelist 校验
    if tool_name not in whitelist:
        try:
            guard.authorize(tool_name, args_signature="__incremental_filter__")
        except ToolPermissionDenied:
            blocked.append({
                "outcome": "whitelist_rejected",
                "tool_name": tool_name,
                "reason": "tool_not_in_whitelist",
            })
            if guard.is_permanently_denied():
                return fail_factory(
                    decision,
                    reason="permanent_permission_denied",
                ), blocked
            return decision, blocked

    # 4. Pydantic schema 校验
    #
    # LLM 决策层面经常会把通用字段 ``target_section_ids`` 放进
    # tool_arguments；而真实工具契约要求 ``section_ids``。这里先做一层
    # 明确的 adapter-style 归一化，再继续走严格 schema，避免合法增量
    # 修复被误判为 schema_invalid 后降级到 legacy 空状态路径。
    normalized_args, normalized_targets = _normalize_tool_arguments(
        tool_name=tool_name,
        args=decision.tool_arguments or {},
        target_section_ids=decision.target_section_ids or [],
        state=state,
    )
    decision = decision.model_copy(update={
        "tool_arguments": normalized_args,
        "target_section_ids": normalized_targets,
    })

    schema_cls = _INCREMENTAL_TOOL_ARG_SCHEMAS.get(tool_name)
    if schema_cls is None:
        blocked.append({
            "outcome": "no_schema",
            "tool_name": tool_name,
            "reason": "no_pydantic_schema",
        })
        return fail_factory(decision, reason="no_tool_schema"), blocked

    try:
        schema_cls.model_validate(decision.tool_arguments or {})
    except Exception as exc:
        blocked.append({
            "outcome": "schema_invalid",
            "tool_name": tool_name,
            "reason": str(exc)[:200],
        })
        return fail_factory(
            decision,
            reason=f"tool_args_schema_invalid:{tool_name}",
        ), blocked

    # 5. scope_guard 校验(独立)
    try:
        enforce_minimal_scope(
            decision,
            scope=scope,
            locked_section_ids=locked_section_ids,
        )
    except ScopeGuardViolation as exc:
        blocked.append({
            "outcome": "scope_guard_violation",
            "tool_name": tool_name,
            "reason": str(exc),
        })
        return fail_factory(
            decision,
            reason=f"scope_guard:{str(exc)[:160]}",
        ), blocked

    # 6. args_signature 计数器(同一 (tool_name, args_signature) ≤ 2)
    sig = _args_signature_for(tool_name, decision.tool_arguments or {})
    try:
        guard.authorize(tool_name, args_signature=sig)
    except PermanentPermissionDenied:
        blocked.append({
            "outcome": "permanent_denied",
            "tool_name": tool_name,
            "reason": "args_signature_repeated",
        })
        return fail_factory(
            decision,
            reason="args_signature_repeated_permanent",
        ), blocked
    except ToolPermissionDenied:
        blocked.append({
            "outcome": "permission_denied",
            "tool_name": tool_name,
            "reason": "args_signature_over_budget",
        })
        return decision, blocked

    return decision, blocked


def _normalize_tool_arguments(
    *,
    tool_name: str,
    args: Dict[str, Any],
    target_section_ids: Iterable[Any],
    state: Dict[str, Any] | None = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Normalize LLM tool args to the exact incremental tool schemas.

    This is intentionally small and explicit: aliases are accepted at the LLM
    boundary, but only schema-approved keys are allowed past this point.
    """
    if not isinstance(args, dict):
        args = {}
    targets = _as_string_list(target_section_ids)

    if tool_name == "TestPlanRegenTool":
        section_ids = _as_string_list(
            args.get("section_ids")
            or args.get("target_section_ids")
            or targets
        )
        issues_raw = args.get("issues")
        issues = [
            issue for issue in (issues_raw if isinstance(issues_raw, list) else [])
            if isinstance(issue, dict)
        ]
        if not issues:
            issues = _incremental_regen_issues_from_state(
                state,
                section_ids,
            )
        generation_config_subset = args.get("generation_config_subset")
        normalized = {
            "section_ids": section_ids,
            "issues": issues,
            "generation_config_subset": (
                generation_config_subset
                if isinstance(generation_config_subset, dict)
                else {}
            ),
        }
        return normalized, _ordered_union(targets, section_ids)

    if tool_name == "ResultReviewTool":
        target_ids = _as_string_list(
            args.get("target_section_ids")
            or args.get("section_ids")
            or targets
        )
        review_standard = args.get("review_standard")
        return {
            "target_section_ids": target_ids,
            "review_standard": review_standard if isinstance(review_standard, dict) else {},
        }, _ordered_union(targets, target_ids)

    if tool_name == "DocxFormatCheckTool":
        target_ids = _as_string_list(
            args.get("target_section_ids")
            or args.get("section_ids")
            or targets
        )
        return {
            "artifact_public_id": str(args.get("artifact_public_id") or ""),
            "target_section_ids": target_ids,
        }, _ordered_union(targets, target_ids)

    if tool_name == "KnowledgeSearchTool":
        labels = args.get("labels")
        top_k = args.get("top_k", 5)
        return {
            "query": str(args.get("query") or ""),
            "top_k": top_k if isinstance(top_k, int) else 5,
            "labels": _as_string_list(labels)[:10],
        }, targets

    if tool_name == "WordExportTool":
        version_no = args.get("version_no")
        if isinstance(version_no, str) and version_no.isdigit():
            version_no = int(version_no)
        return {
            "artifact_public_id": str(args.get("artifact_public_id") or ""),
            "version_no": version_no,
            "template_file_id": str(args.get("template_file_id") or ""),
        }, targets

    return dict(args), targets


def _as_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in (None, "")]
    return []


def _incremental_regen_issues_from_state(
    state: Dict[str, Any] | None,
    section_ids: List[str],
) -> List[Dict[str, Any]]:
    """Fill the regen contract when the LLM omits ``issues``.

    Incremental requests often start from the user's modification text rather
    than a prior ResultReviewTool call. The tool still requires a non-empty
    issue list, so derive it from the latest review when available and fall
    back to one scoped user-request issue per target section.
    """
    if not isinstance(state, dict) or not section_ids:
        return []

    review = state.get("review_result")
    if isinstance(review, dict):
        review_issues = review.get("review_issues")
        if isinstance(review_issues, list) and review_issues:
            blocking = [
                issue
                for issue in review_issues
                if isinstance(issue, dict) and issue.get("severity") == "block"
            ]
            matched = _issues_for_targets(blocking, section_ids)
            if matched:
                return matched

        for key in ("block_issues", "issues"):
            candidates = review.get(key)
            if isinstance(candidates, list):
                matched = _issues_for_targets(
                    [issue for issue in candidates if isinstance(issue, dict)],
                    section_ids,
                )
                if matched:
                    return matched

    intent = state.get("incremental_intent")
    scope = _value_from_object(intent, "scope")
    request_text = _value_from_object(scope, "request_text") or _value_from_object(
        intent,
        "raw_user_message",
    )
    request_text = str(request_text or "按增量任务要求重写目标章节")[:2000]

    issues: List[Dict[str, Any]] = []
    for requested_id in section_ids:
        binding, aliases = _find_incremental_binding(state, requested_id)
        canonical_id = str(
            (binding or {}).get("section_id") or requested_id
        )
        field = str((binding or {}).get("field") or "")
        title = str((binding or {}).get("title") or "")
        evidence: Dict[str, Any] = {
            "section_id": canonical_id,
            "aliases": aliases,
        }
        if field:
            evidence["field"] = field
        if title:
            evidence["title"] = title
        issues.append({
            "issue_id": f"incremental_modify_{requested_id}",
            "rule_id": "incremental_user_request",
            "section_id": canonical_id,
            "requested_section_id": requested_id,
            "field_path": field or None,
            "severity": "block",
            "message": request_text,
            "repairable": True,
            "suggested_strategy": "regenerate_section",
            "kind": "incremental_modification",
            "evidence": evidence,
        })
    return issues


def _issues_for_targets(
    issues: List[Dict[str, Any]],
    section_ids: List[str],
) -> List[Dict[str, Any]]:
    targets = {str(value) for value in section_ids if value}
    if not targets:
        return []
    matched: List[Dict[str, Any]] = []
    for issue in issues:
        values = {
            str(issue.get(key) or "")
            for key in (
                "section_id",
                "requested_section_id",
                "field",
                "field_path",
                "title",
            )
            if issue.get(key)
        }
        evidence = issue.get("evidence")
        if isinstance(evidence, dict):
            for key in (
                "section_id",
                "requested_section_id",
                "field",
                "field_path",
                "title",
                "aliases",
            ):
                value = evidence.get(key)
                if isinstance(value, list):
                    values.update(str(item) for item in value if item)
                elif value:
                    values.add(str(value))
        if values & targets:
            matched.append(dict(issue))
    return matched


def _find_incremental_binding(
    state: Dict[str, Any],
    requested_id: str,
) -> Tuple[Dict[str, Any] | None, List[str]]:
    aliases = {str(requested_id)}
    confirm = state.get("section_confirm_config")
    confirm_sections = confirm.get("sections") if isinstance(confirm, dict) else []
    if isinstance(confirm_sections, list):
        for section in confirm_sections:
            if not isinstance(section, dict):
                continue
            values = {
                str(section.get(key) or "")
                for key in ("id", "section_id", "field", "title")
                if section.get(key)
            }
            if requested_id in values:
                aliases.update(values)

    template = state.get("template_structure")
    generation_config = (
        template.get("generation_config")
        if isinstance(template, dict)
        else None
    ) or {}
    ai_fields = generation_config.get("ai_fields") or []
    if isinstance(ai_fields, list):
        for entry in ai_fields:
            if not isinstance(entry, dict):
                continue
            values = {
                str(entry.get(key) or "")
                for key in ("field", "section_id", "id", "title")
                if entry.get(key)
            }
            if aliases & values:
                aliases.update(values)
                return entry, sorted(aliases)
    return None, sorted(aliases)


def _value_from_object(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _ordered_union(*groups: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    ordered: List[str] = []
    for group in groups:
        for item in group:
            if item and item not in seen:
                seen.add(item)
                ordered.append(item)
    return ordered


def _args_signature_for(tool_name: str, args: Dict[str, Any]) -> str:
    """稳定哈希:tool_name + 排序后的 args JSON。

    与 ``_shared.args_signature.args_signature`` 同形;这里独立实现以避免循环。
    """
    import hashlib
    import json

    canonical = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    raw = f"{tool_name}|{canonical}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


__all__ = [
    "TestPlanRegenArgs",
    "ResultReviewArgs",
    "KnowledgeSearchArgs",
    "WordExportArgs",
    "DocxFormatCheckArgs",
    "filter_incremental_decision",
]

# module-level note (auto-appended):
# Incremental 决策过滤。
# 关键约束: 与 prep 同工具白名单 + scope_guard。
