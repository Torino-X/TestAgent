"""Repair Agent 主循环 (Phase 2.4 — ADR-2.4-11 + Rule 4/14).

镜像 ``preparation.agent_loop.run_preparation`` 但适配 Repair:

* 6 节点不是 5: 加一个 ``repair_re_review``(单次 repair 内可多次调
  ``ResultReviewTool`` 看是否仍 block)
* 收尾条件:所有 block issues 都解决 OR budget 耗尽 OR 异常
* 永不抛:失败合成 best-effort ``RepairResult`` + ``emit REPAIR_FALLBACK``
* Mode A (Structured Action JSON) 唯一通路;Mode B TODO stub 与 prep 一致
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from app.agent_runtime._shared.args_signature import args_signature
from app.agent_runtime._shared.budget import BudgetExceeded, BudgetTracker
from app.agent_runtime.repair.budget import BudgetExceeded as _BE  # noqa: F401
from app.agent_runtime.repair.capabilities import ModelCapabilities, resolve_capabilities
from app.agent_runtime.repair.decision_filter import filter_repair_decision
from app.agent_runtime.repair.event_emitter import RepairEventEmitter
from app.agent_runtime.repair.issue_parser import parse_review_issues
from app.agent_runtime.repair.prompt import REPAIR_SYSTEM_PROMPT, build_repair_prompt
from app.agent_runtime.repair.permission import REPAIR_TOOL_WHITELIST, ToolPermissionGuard
from app.agent_runtime.repair.schemas import (
    BudgetState,
    KnowledgeEvidence,
    PublicSummary,
    RepairDecision,
    RepairResult,
    ReviewIssue,
)
from app.context_engine.errors import ContextEngineFailure


logger = logging.getLogger(__name__)


# Repair Agent budget: 比 prep 略宽松(章节修复上下文大)
_MAX_REPAIR_STEPS = 8
_MAX_REPAIR_TOOL_CALLS = 6
_MAX_REPAIR_WALL_TIME = 120.0
_MAX_REPAIR_TOKEN_ESTIMATE = 8000
_MAX_SAME_TOOL_SAME_ARGS = 2


class _RepairBudgetLimits:
    """Mirror of preparation.budget.BudgetTracker constants (Phase 2.4)."""

    MAX_AGENT_STEPS = _MAX_REPAIR_STEPS
    MAX_TOOL_CALLS = _MAX_REPAIR_TOOL_CALLS
    MAX_WALL_TIME_SECONDS = _MAX_REPAIR_WALL_TIME
    MAX_TOKEN_ESTIMATE = _MAX_REPAIR_TOKEN_ESTIMATE
    MAX_SAME_TOOL_SAME_ARGS = _MAX_SAME_TOOL_SAME_ARGS


# ── Helpers ─────────────────────────────────────────────────────────────


def _now() -> float:
    return time.monotonic()


def _safe_json_loads(raw: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        return None


def _issue_id_set(issues: List[ReviewIssue]) -> "set[str]":
    return {i.issue_id for i in issues}


def _issue_details(
    issues: List[ReviewIssue],
    issue_ids: List[str],
) -> List[Dict[str, Any]]:
    wanted = set(issue_ids or [])
    details: List[Dict[str, Any]] = []
    for issue in issues:
        if wanted and issue.issue_id not in wanted:
            continue
        details.append(
            {
                "issue_id": issue.issue_id,
                "section_id": issue.section_id,
                "message": issue.message,
                "evidence": issue.evidence,
                "rule_id": issue.rule_id,
                "kind": issue.kind,
            }
        )
    return details


def _format_issue_details(details: List[Dict[str, Any]], limit: int = 3) -> str:
    if not details:
        return "仍有审查问题未完成闭环验证。"
    parts: List[str] = []
    for item in details[:limit]:
        section = str(item.get("section_id") or "未定位章节")[:120]
        message = str(item.get("message") or "审查未通过")[:160]
        parts.append(f"**{section}**: {message}")
    more = len(details) - len(parts)
    if more > 0:
        parts.append(f"另有 {more} 个问题待确认")
    return "；".join(parts)


def _format_unresolved_repair_summary(
    *,
    fallback_reason: str | None,
    remaining_issue_summary: str,
) -> str:
    reason = fallback_reason or ""
    if reason.startswith("budget_exhausted:"):
        prefix = "修复预算已耗尽，仍有阻断问题未通过复审。"
    elif reason.startswith("decision_filter_fail:"):
        prefix = "RepairAgent 决策未通过范围校验，修复工具尚未执行。"
    elif reason.startswith("tool_failed:") or reason.startswith("tool_execute_error:"):
        prefix = "RepairAgent 调用修复工具失败，仍有阻断问题未解决。"
    elif reason.startswith("re_review_"):
        prefix = "RepairAgent 已执行修复动作，但复审未完成。"
    else:
        prefix = "RepairAgent 未完成复审闭环，仍有阻断问题未解决。"
    return f"{prefix}待关注章节: {remaining_issue_summary}"


def _json_truncation_recovery_meta(state: Dict[str, Any]) -> Dict[str, Any]:
    content = state.get("test_plan_content")
    if not isinstance(content, dict):
        return {}
    recovery = content.get("generation_recovery")
    if isinstance(recovery, dict):
        if recovery.get("kind") != "json_truncated":
            return {}
        if recovery.get("requires_bulk_repair") is False:
            return {}
        return recovery

    # Defense-in-depth: older checkpoints or normalization bugs may have kept
    # schema_issues while dropping generation_recovery.  The generator marks
    # truncation-caused schema issues explicitly, so rebuild the minimal recovery
    # meta from that authoritative review input instead of falling back to the
    # ordinary three-section RepairAgent loop.
    schema_issues = content.get("schema_issues")
    if not isinstance(schema_issues, list):
        return {}
    missing_fields: list[str] = []
    missing_section_ids: list[str] = []
    for issue in schema_issues:
        if not isinstance(issue, dict):
            continue
        if issue.get("source_error") != "json_truncated":
            continue
        field = str(issue.get("field") or "").strip()
        section_id = str(issue.get("section_id") or field).strip()
        if field and field not in missing_fields:
            missing_fields.append(field)
        if section_id and section_id not in missing_section_ids:
            missing_section_ids.append(section_id)
    if not missing_fields and not missing_section_ids:
        return {}
    return {
        "kind": "json_truncated",
        "mode": "reconstructed_from_schema_issues",
        "missing_fields": missing_fields,
        "missing_section_ids": missing_section_ids,
        "missing_count": len(missing_fields or missing_section_ids),
        "requires_bulk_repair": True,
    }


def _issue_source_error(issue: ReviewIssue) -> str:
    try:
        evidence = json.loads(issue.evidence or "{}")
    except Exception:
        evidence = {}
    if isinstance(evidence, dict) and evidence.get("source_error"):
        return str(evidence.get("source_error"))
    return ""


def _build_json_truncation_recovery_decision(
    *,
    state: Dict[str, Any],
    review_issues: List[ReviewIssue],
    locked_section_ids: List[str],
) -> RepairDecision | None:
    """Create a deterministic bulk repair decision for systemic JSON truncation.

    A truncated top-level generation is not a normal local defect.  The review
    issues are still the authority for scope, but the fix should be one bulk
    recovery action so it cannot be starved by the ordinary three-section
    RepairAgent decision budget.
    """
    recovery = _json_truncation_recovery_meta(state)

    wanted_fields = {str(v) for v in recovery.get("missing_fields") or [] if v}
    wanted_sections = {
        str(v) for v in recovery.get("missing_section_ids") or [] if v
    }
    locked = set(locked_section_ids or [])
    selected: list[ReviewIssue] = []
    section_ids: list[str] = []
    for issue in review_issues:
        if issue.section_id and issue.section_id in locked:
            continue
        issue_keys = {
            str(v)
            for v in (issue.section_id, issue.field_path)
            if v
        }
        source_is_truncation = _issue_source_error(issue) == "json_truncated"
        if not recovery and not source_is_truncation:
            continue
        if not source_is_truncation and wanted_fields and not (issue_keys & wanted_fields):
            if not (wanted_sections and issue.section_id in wanted_sections):
                continue
        if not issue.section_id:
            continue
        selected.append(issue)
        if issue.section_id not in section_ids:
            section_ids.append(issue.section_id)

    if not selected or not section_ids:
        return None

    issue_dicts = [issue.model_dump() for issue in selected]
    section_count = len(section_ids)
    return RepairDecision(
        action="call_tool",
        tool_name="TestPlanRegenTool",
        tool_arguments={
            "section_ids": section_ids,
            "issues": issue_dicts,
            "test_plan_content": state.get("test_plan_content") or {},
            "template_structure": state.get("template_structure") or {},
            "recovery_mode": "json_truncated",
            "bulk_repair": True,
        },
        target_issue_ids=[issue.issue_id for issue in selected],
        target_section_ids=section_ids,
        suggested_strategy="bulk_regenerate_missing_sections",
        decision_summary=(
            f"检测到测试方案 JSON 截断导致 {section_count} 个章节缺失，"
            "本轮按批量恢复策略重写所有受影响章节。"
        )[:500],
        expected_result="完成截断缺失章节恢复后自动触发 ResultReviewTool 复审。",
        confidence=1.0,
    )


def _apply_regen_output_to_state(state: Dict[str, Any], data: Dict[str, Any]) -> None:
    """Write regenerated section payloads back to Graph State explicitly.

    TestPlanRegenTool mutates the AgentContext proxy in place in production, but
    the repair loop should not depend on object identity between the proxy and
    Graph State.  When the tool returns section payloads, splice them here too.
    """
    if not isinstance(data, dict):
        return
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        return
    content = state.get("test_plan_content")
    if not isinstance(content, dict):
        return

    resolved_fields = {
        str(v)
        for sec in sections
        if isinstance(sec, dict)
        for v in (
            sec.get("section_id"),
            sec.get("field"),
            sec.get("id"),
            sec.get("title"),
        )
        if v
    }
    for key in (
        "regenerated_section_ids",
        "section_ids",
        "resolved_schema_issue_fields",
    ):
        values = data.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if value:
                resolved_fields.add(str(value))

    def _splice_sections(generated: List[Any]) -> List[Any]:
        by_key: Dict[str, int] = {}
        for idx, sec in enumerate(generated):
            if not isinstance(sec, dict):
                continue
            for key in ("section_id", "field", "id", "title"):
                value = sec.get(key)
                if value:
                    by_key.setdefault(str(value), idx)
        updated = list(generated)
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            keys = [
                str(sec.get(k))
                for k in ("section_id", "field", "id", "title")
                if sec.get(k)
            ]
            hit = next((by_key[k] for k in keys if k in by_key), None)
            if hit is not None and isinstance(updated[hit], dict):
                updated[hit] = dict(updated[hit], **sec)
            else:
                by_key.update({k: len(updated) for k in keys if k})
                updated.append(dict(sec))
        return updated

    package = content.get("section_package")
    if isinstance(package, dict):
        generated = package.get("generated_sections")
        if isinstance(generated, list):
            generated = _splice_sections(generated)
            package["generated_sections"] = generated
            content["section_package"] = package
            if not isinstance(content.get("generated_sections"), list):
                content["generated_sections"] = len(generated)

    top_level_generated = content.get("generated_sections")
    if isinstance(top_level_generated, list):
        content["generated_sections"] = _splice_sections(top_level_generated)

    legacy_sections = content.get("sections")
    if isinstance(legacy_sections, list):
        content["sections"] = _splice_sections(legacy_sections)

    schema_issues = content.get("schema_issues")
    if isinstance(schema_issues, list) and schema_issues and resolved_fields:
        remaining_schema_issues: List[Any] = []
        for issue in schema_issues:
            if not isinstance(issue, dict):
                remaining_schema_issues.append(issue)
                continue
            issue_keys = {
                str(v)
                for v in (
                    issue.get("field"),
                    issue.get("section_id"),
                    issue.get("id"),
                    issue.get("title"),
                )
                if v
            }
            if issue_keys and issue_keys & resolved_fields:
                continue
            remaining_schema_issues.append(issue)
        if remaining_schema_issues:
            content["schema_issues"] = remaining_schema_issues
        else:
            content.pop("schema_issues", None)

    recovery = content.get("generation_recovery")
    if isinstance(recovery, dict) and recovery.get("kind") == "json_truncated":
        fields = [
            str(v)
            for v in recovery.get("missing_fields") or []
            if v and str(v) not in resolved_fields
        ]
        sections = [
            str(v)
            for v in recovery.get("missing_section_ids") or []
            if v and str(v) not in resolved_fields
        ]
        if fields or sections:
            recovery["missing_fields"] = fields
            recovery["missing_section_ids"] = sections
            recovery["missing_count"] = len(fields)
            content["generation_recovery"] = recovery
        else:
            content.pop("generation_recovery", None)


def _apply_review_envelope(
    state: Dict[str, Any],
    data: Dict[str, Any],
    *,
    locked_section_ids: List[str],
    issues_remaining: List[str],
    issues_resolved: List[str],
    issues_resolved_set: "set[str]",
) -> tuple[bool, List[ReviewIssue], List[str]]:
    review = dict(state.get("review_result") or {})
    if isinstance(data, dict):
        review.update(data)
    state["review_result"] = review

    level = str((data or {}).get("level") or review.get("level") or "").lower()
    next_issues = parse_review_issues(
        review,
        locked_section_ids=locked_section_ids,
    )
    if level == "passed" or not next_issues:
        review["level"] = "passed"
        review["review_issues"] = []
        review["block_issues"] = []
        review["issues"] = []
        state["review_result"] = review
        for issue_id in list(issues_remaining):
            if issue_id not in issues_resolved_set:
                issues_resolved.append(issue_id)
                issues_resolved_set.add(issue_id)
        return True, [], []

    next_ids = _issue_id_set(next_issues)
    for issue_id in list(issues_remaining):
        if issue_id not in next_ids and issue_id not in issues_resolved_set:
            issues_resolved.append(issue_id)
            issues_resolved_set.add(issue_id)
    return False, next_issues, list(next_ids)


# ── Main loop ───────────────────────────────────────────────────────────


async def run_repair(
    state: Dict[str, Any],
    *,
    llm_client,
    tool_adapter,
    ctx,
    capabilities: Optional[ModelCapabilities] = None,
    clock: Callable[[], float] = _now,
) -> RepairResult:
    """主 Repair 循环 (永不抛 — Rule 14)。

    Args:
        state: ``TestPlanGraphState`` 字典
        llm_client: 需提供 ``generate_with_profile(profile, user, parser=...)``
        tool_adapter: 需提供 ``await execute(tool_name, inputs, ctx_runtime=...)``
        ctx: ``RuntimeContext``
        capabilities: ``ModelCapabilities`` (None → ``resolve_capabilities()``)
        clock: 可注入 (测试)

    Returns:
        ``RepairResult`` (review_passed + issues_resolved + ...)
    """
    caps = capabilities or resolve_capabilities()
    emitter = RepairEventEmitter(ctx)
    budget = BudgetTracker(
        wall_clock_fn=clock,
        max_steps=_MAX_REPAIR_STEPS,
        max_tool_calls=_MAX_REPAIR_TOOL_CALLS,
        max_wall_seconds=_MAX_REPAIR_WALL_TIME,
        max_token_estimate=_MAX_REPAIR_TOKEN_ESTIMATE,
    )
    guard = ToolPermissionGuard(whitelist=REPAIR_TOOL_WHITELIST)

    review_issues = parse_review_issues(
        state.get("review_result") or {},
        locked_section_ids=state.get("locked_section_ids") or [],
    )
    locked_section_ids = list(state.get("locked_section_ids") or [])

    # Mode B TODO (mirror preparation pytest.skip)
    if getattr(caps, "native_tool_calling", False):
        logger.warning(
            "Repair Agent: native_tool_calling=True requested; "
            "Mode B is TODO — skipping to legacy fallback"
        )
        return _build_fallback_result(
            budget=budget,
            reason="mode_b_deferred",
            loop_count=int(state.get("repair_loop_count") or 0),
        )

    await emitter.emit_repair_started(
        issue_count=len(review_issues),
        loop_count=int(state.get("repair_loop_count") or 0),
    )

    steps_audit: List[Dict[str, Any]] = []
    knowledge_evidence: List[KnowledgeEvidence] = []
    issues_resolved: List[str] = []
    issues_remaining: List[str] = [i.issue_id for i in review_issues]
    issues_resolved_set: "set[str]" = set()
    modified_sections: List[str] = []
    rounds_used = 0
    tool_calls_used = 0
    review_passed = False
    last_fallback_reason: Optional[str] = None

    import uuid
    run_id = f"repair-{uuid.uuid4().hex[:8]}"

    try:
        for step in range(_MAX_REPAIR_STEPS):
            budget.on_step()
            rounds_used = step + 1

            decision = _build_json_truncation_recovery_decision(
                state=state,
                review_issues=review_issues,
                locked_section_ids=locked_section_ids,
            )
            if decision is None:
                # Build prompt
                try:
                    system, user_content = build_repair_prompt(
                        review_issues=[
                            i.model_dump() for i in review_issues
                        ],
                        locked_section_ids=locked_section_ids,
                        test_plan_content_excerpt=state.get("test_plan_content"),
                        prior_steps=steps_audit,
                        capabilities=caps,
                        mode="mode_a",
                    )
                except Exception as exc:
                    logger.warning("repair prompt build failed: %s", exc)
                    last_fallback_reason = "prompt_build_error"
                    break

                budget.check()  # 可能 raise BudgetExceeded

                # LLM call (mirror preparation/agent_loop._llm_decide pattern)
                # CE-04 WP-10 / 整改 §三：MIG_REPAIR flag 决定路由。
                #   MIG_REPAIR=false → 明确走 legacy（llm_client.generate_with_profile）
                #   MIG_REPAIR=true  → 只走 ContextInvokerBridge；失败执行 failure policy，
                #                      不静默回退 legacy LLMClient。
                from app.llm.task_profiles import get_builtin_profile

                from app.context_engine.feature_flags import require_agent_context_migration

                # CE-05 WP-2：任务路径经 ctx.task_flag_resolver 读 MIG_REPAIR（冻结 Manifest）；
                # resolver 缺失/损坏 → fail closed，不读取进程级 MIG 配置。
                resolver = getattr(ctx, "task_flag_resolver", None)
                migration_error = require_agent_context_migration(
                    resolver, "MIG_REPAIR"
                )
                if migration_error is not None:
                    last_fallback_reason = migration_error.lower()
                    break
                try:
                    bridge = getattr(ctx, "context_llm_invoker", None)
                    if bridge is None or not getattr(bridge, "available", False):
                        logger.warning(
                            "repair Context Engine invoker unavailable | task_id=%s",
                            getattr(ctx, "task_internal_id", None),
                        )
                        last_fallback_reason = "llm_call_error"
                        break
                    repair_state_ref = _task_state_ref(state)
                    repair_context_manifest = _repair_context_manifest(repair_state_ref)
                    bres = await bridge.generate(
                        user_id=ctx.user_internal_id,
                        call_site="test_plan.repair.plan",
                        llm_task_profile=get_builtin_profile("repair_agent"),
                        current_node="repair_plan",
                        current_goal=user_content,
                        task_state_ref=repair_state_ref,
                        output_contract="repair_decision_json",
                        system_prompt=system,
                        user_content=user_content,
                        conversation_id=ctx.conversation_internal_id,
                        task_id=ctx.task_internal_id,
                        runtime_context=ctx,
                    )
                    raw = bres.as_profile_result() if bres is not None else None
                    if raw is None:
                        last_fallback_reason = "llm_call_error"
                        break
                    if False:  # removed legacy direct-LLM fallback; kept temporarily for source compatibility
                        if bridge is None or not getattr(bridge, "available", False):
                            # Invoker 未构建（degraded）→ 显式 failure，不伪造 available
                            logger.warning(
                                "repair MIG_REPAIR=true 但 Invoker 不可用 | task_id=%s",
                                getattr(ctx, "task_internal_id", None),
                            )
                            last_fallback_reason = "llm_call_error"
                            break
                        bres = await bridge.generate(
                            user_id=ctx.user_internal_id,
                            call_site="test_plan.repair.plan",
                            llm_task_profile=get_builtin_profile("repair_agent"),
                            current_node="repair_plan",
                            current_goal=user_content,
                            task_state_ref=_task_state_ref(state),
                            output_contract="repair_decision_json",
                            user_content=user_content,
                            conversation_id=ctx.conversation_internal_id,
                            task_id=ctx.task_internal_id,
                            runtime_context=ctx,
                        )
                        raw = bres.as_profile_result() if bres is not None else None
                        if raw is None:
                            last_fallback_reason = "llm_call_error"
                            break
                    elif False:  # legacy direct-LLM branch is retired
                        # Phase 2.9A.X bug fix：之前硬编码 ``parser="json_strict"``
                        # （字符串），而 ``generate_with_profile`` 的 ``parser``
                        # 参数期望 ParserAdapter 实例（注释明确写
                        # ``# ParserAdapter (overrides registry)``）。字符串是
                        # truthy，导致 ``adapter = parser or get_parser(...)``
                        # 走到 ``adapter = "json_strict"``，调用 .parse() 抛
                        # ``'str' object has no attribute 'parse'``。
                        #
                        # REPAIR_AGENT_PROFILE 已设 ``parser=LLMParserType.JSON_STRICT``，
                        # 传 None 让 llm_client 走 ``get_parser(profile.parser)`` 自动取。
                        raise RuntimeError("legacy repair LLM path is retired")
                except ContextEngineFailure as exc:
                    safe_metadata = dict(exc.error.safe_metadata or {})
                    required_section = str(safe_metadata.get("section_id") or "unknown")
                    diagnostic = {
                        "code": exc.error.code,
                        "stage": exc.error.stage.value,
                        "required_section": required_section,
                        "input_manifest": repair_context_manifest,
                    }
                    if isinstance(state, dict):
                        state["repair_context_diagnostic"] = diagnostic
                    logger.warning(
                        "repair Context Engine selection failed | code=%s stage=%s "
                        "required_section=%s task_id=%s input_manifest=%s",
                        exc.error.code,
                        exc.error.stage.value,
                        required_section,
                        getattr(ctx, "task_internal_id", None),
                        repair_context_manifest,
                    )
                    last_fallback_reason = (
                        f"context_selection_required_unmet:{required_section}"
                    )
                    break
                except Exception as exc:
                    logger.warning("repair LLM call failed: %s", exc)
                    last_fallback_reason = "llm_call_error"
                    break

                # LLMClient.generate_with_profile returns an object with .parsed (dict|None)
                # and .raw_text (str). _FakeResult in tests follows the same shape.
                parsed = getattr(raw, "parsed", None)
                if not getattr(raw, "success", False) or parsed is None:
                    err_msg = str(getattr(raw, "error_message", "") or "LLM parse failed")
                    logger.warning("repair LLM parse failed: %s", err_msg)
                    last_fallback_reason = "llm_parse_error"
                    break

                # parsed 可能是 str (fallback_text) 或 dict (JSON 解析成功)
                if isinstance(parsed, str):
                    try:
                        parsed = _safe_json_loads(parsed)
                    except Exception:
                        parsed = None
                if not isinstance(parsed, dict):
                    last_fallback_reason = "llm_parse_error"
                    break
                try:
                    decision = RepairDecision.model_validate(parsed)
                except Exception as exc:
                    logger.warning("repair decision validation failed: %s", exc)
                    last_fallback_reason = "decision_validation_error"
                    break

            # Phase 2.9A.X: LLM 的 decision_summary 偶尔超过 500 字符
            # (prompt 约束 ≤500 但不总是遵守)。model_validate 可能宽松通过，
            # 但后续 filter_repair_decision 构造新 RepairDecision 时
            # Pydantic __init__ 严格校验 → ValidationError → repair fallback。
            # 预防性截断确保后续链路不会因 summary 过长而崩溃。
            if len(decision.decision_summary) > 500:
                decision.decision_summary = decision.decision_summary[:500]

            # Audit step
            sig = args_signature(decision.tool_arguments or {})
            steps_audit.append(
                {
                    "decision_summary": decision.decision_summary,
                    "tool_name": decision.tool_name or "-",
                    "args_signature": sig,
                    "target_issue_ids": list(decision.target_issue_ids or []),
                    "target_section_ids": list(decision.target_section_ids or []),
                    "outcome": "pending",
                }
            )
            await emitter.emit_decision_update(
                step_index=len(steps_audit) - 1,
                decision=decision,
            )

            # Phase 2.9A.X 诊断：记录决策的 action 和关键字段，排查
            # RepairAgent 为何在 emit_decision_update 后直接 fallback
            # 而不调用 TestPlanRegenTool。
            logger.warning(
                "repair decision: action=%s | tool_name=%s | "
                "target_sections=%s | summary_len=%d | decision_summary=%s",
                decision.action,
                decision.tool_name,
                decision.target_section_ids,
                len(decision.decision_summary or ""),
                (decision.decision_summary or "")[:100],
            )

            # action=fail → loop break
            if decision.action == "fail":
                steps_audit[-1]["outcome"] = "fail"
                last_fallback_reason = f"model_decided_fail:{decision.decision_summary[:80]}"
                break

            # action=finish → 检查是否所有 issues 都解决
            if decision.action == "finish":
                steps_audit[-1]["outcome"] = "finish"
                # 把 declared target_issue_ids 视为已解决
                resolved_now = set(decision.target_issue_ids or [])
                for tid in resolved_now:
                    if tid in issues_remaining:
                        issues_resolved.append(tid)
                        issues_resolved_set.add(tid)
                issues_remaining = [
                    i for i in issues_remaining
                    if i not in issues_resolved_set
                ]
                review_passed = not any(
                    i.severity == "block" for i in review_issues
                    if i.issue_id in issues_remaining
                )
                break

            # action=call_tool
            clean_decision, blocked = filter_repair_decision(
                decision,
                guard=guard,
                review_issues=review_issues,
                locked_section_ids=locked_section_ids,
            )
            logger.warning(
                "repair filter_repair_decision: clean_action=%s | "
                "blocked=%s | tool_name=%s",
                clean_decision.action,
                [b.get("reason", "?") for b in blocked] if blocked else [],
                clean_decision.tool_name,
            )
            if clean_decision.action == "fail":
                steps_audit[-1]["outcome"] = "blocked"
                steps_audit[-1]["blocked"] = blocked
                last_fallback_reason = (
                    f"decision_filter_fail:{blocked[0].get('reason', 'unknown')}"
                )
                break

            budget.on_tool_call()
            if budget.snapshot().tool_calls > _MAX_REPAIR_TOOL_CALLS:
                last_fallback_reason = "tool_call_budget_exhausted"
                break

            tool_name = clean_decision.tool_name or ""
            inputs = clean_decision.tool_arguments or {}
            logger.warning(
                "repair EXECUTING tool: tool=%s | inputs_keys=%s | "
                "section_ids=%s | has_generation_config=%s",
                tool_name,
                sorted(inputs.keys()) if isinstance(inputs, dict) else "?",
                inputs.get("section_ids") if isinstance(inputs, dict) else "?",
                bool(inputs.get("generation_config_subset")) if isinstance(inputs, dict) else "?",
            )
            try:
                # Phase 2.9A.X bug fix: 必须传 graph_state=state,
                # 否则 _build_proxy 走 ctx._intermediate_state（空 dict,
                # 业务字段不持久化），TestPlanRegenTool 拿不到
                # context.test_plan_content → REGEN_NO_EXISTING 失败，
                # RepairAgent 反复重试仍失败。
                envelope = await tool_adapter.execute(
                    tool_name=tool_name,
                    inputs=inputs,
                    ctx_runtime=ctx,
                    graph_state=state,
                )
            except Exception as exc:
                logger.warning("repair tool execute failed: %s", exc)
                last_fallback_reason = f"tool_execute_error:{type(exc).__name__}"
                break

            tool_calls_used += 1
            data = (envelope or {}).get("data") or {}
            success = bool(envelope and envelope.get("success"))
            error = (envelope or {}).get("error") or {}
            # Phase 2.9A.X 诊断：RepairAgent 调用工具失败时记录完整 inputs + error，
            # 定位 TestPlanRegenTool 反复失败的根因（adapter 拒绝 or 工具内部错误）。
            if not success:
                logger.warning(
                    "repair tool FAILED | tool=%s | error_code=%s | "
                    "error_msg=%s | inputs_keys=%s | summary=%s",
                    tool_name,
                    error.get("code") or "?",
                    str(error.get("message") or "")[:200],
                    sorted(inputs.keys()) if isinstance(inputs, dict) else "?",
                    str((envelope or {}).get("summary") or "")[:200],
                )
            steps_audit[-1]["outcome"] = (
                "ok" if success else "tool_failed"
            )
            await emitter.emit_observation_update(
                step_index=len(steps_audit) - 1,
                tool_name=tool_name,
                success=bool(envelope and envelope.get("success")),
                summary=str(
                    (envelope or {}).get("summary")
                    or data.get("summary")
                    or error.get("message")
                    or ("Tool completed." if envelope and envelope.get("success") else "Tool failed.")
                )[:240],
                error_code=str(error.get("code")) if error.get("code") else None,
            )

            if tool_name == "KnowledgeSearchTool":
                for chunk in data.get("chunks") or []:
                    knowledge_evidence.append(
                        KnowledgeEvidence(
                            query=str(inputs.get("query") or "")[:240],
                            snippet=str(
                                chunk.get("text") or chunk.get("content") or ""
                            )[:400],
                            source=chunk.get("title") or chunk.get("source"),
                            relevance=chunk.get("relevance"),
                        )
                    )

            elif tool_name == "TestPlanRegenTool":
                if not success:
                    last_fallback_reason = (
                        f"tool_failed:{error.get('code') or 'TestPlanRegenTool'}"
                    )
                    break

                _apply_regen_output_to_state(state, data)

                # 记录已修改的 sections
                changed = (
                    data.get("regenerated_section_ids")
                    or data.get("section_ids")
                    or clean_decision.target_section_ids
                    or []
                )
                for sid in changed:
                    if sid not in modified_sections:
                        modified_sections.append(str(sid))

                budget.on_tool_call()
                if budget.snapshot().tool_calls > _MAX_REPAIR_TOOL_CALLS:
                    last_fallback_reason = "tool_call_budget_exhausted"
                    break

                review_step = {
                    "decision_summary": "重写后自动复审",
                    "tool_name": "ResultReviewTool",
                    "args_signature": args_signature({}),
                    "target_issue_ids": list(issues_remaining),
                    "target_section_ids": list(modified_sections),
                    "outcome": "pending",
                    "auto_re_review": True,
                }
                steps_audit.append(review_step)
                try:
                    review_envelope = await tool_adapter.execute(
                        tool_name="ResultReviewTool",
                        inputs={},
                        ctx_runtime=ctx,
                        graph_state=state,
                    )
                except Exception as exc:
                    logger.warning("repair automatic re-review failed: %s", exc)
                    last_fallback_reason = (
                        f"re_review_execute_error:{type(exc).__name__}"
                    )
                    break

                tool_calls_used += 1
                review_data = (review_envelope or {}).get("data") or {}
                review_success = bool(
                    review_envelope and review_envelope.get("success")
                )
                review_error = (review_envelope or {}).get("error") or {}
                review_step["outcome"] = "ok" if review_success else "tool_failed"
                await emitter.emit_observation_update(
                    step_index=len(steps_audit) - 1,
                    tool_name="ResultReviewTool",
                    success=review_success,
                    summary=str(
                        (review_envelope or {}).get("summary")
                        or review_data.get("summary")
                        or review_error.get("message")
                        or (
                            "Tool completed."
                            if review_success else "Tool failed."
                        )
                    )[:240],
                    error_code=(
                        str(review_error.get("code"))
                        if review_error.get("code") else None
                    ),
                )
                if not review_success:
                    last_fallback_reason = (
                        f"re_review_failed:{review_error.get('code') or 'unknown'}"
                    )
                    break

                review_passed, review_issues, issues_remaining = _apply_review_envelope(
                    state,
                    review_data,
                    locked_section_ids=locked_section_ids,
                    issues_remaining=issues_remaining,
                    issues_resolved=issues_resolved,
                    issues_resolved_set=issues_resolved_set,
                )
                if review_passed:
                    break

            elif tool_name == "ResultReviewTool":
                # 单次 repair 内的 re-review: 把新 issues 合并到 remaining;
                # 若 review level=passed 或 review_issues 空 → 把所有当前 remaining
                # 视为已解决(review envelope 是权威判定)。
                if not success:
                    last_fallback_reason = (
                        f"tool_failed:{error.get('code') or 'ResultReviewTool'}"
                    )
                    break
                review_passed, review_issues, issues_remaining = _apply_review_envelope(
                    state,
                    data,
                    locked_section_ids=locked_section_ids,
                    issues_remaining=issues_remaining,
                    issues_resolved=issues_resolved,
                    issues_resolved_set=issues_resolved_set,
                )
                if review_passed:
                    break

            budget.check()

        # End for

    except BudgetExceeded as exc:
        last_fallback_reason = f"budget_exhausted:{exc.code}"
        await emitter.emit_repair_budget_exhausted(
            code=exc.code,
            steps=budget.snapshot().steps,
            tool_calls=budget.snapshot().tool_calls,
            wall_seconds=budget.snapshot().wall_seconds,
        )
    except Exception as exc:  # 兜底永不抛
        logger.exception("repair loop crash: %s", exc)
        last_fallback_reason = f"loop_exception:{type(exc).__name__}"

    if not review_passed and not last_fallback_reason:
        # 自然退出循环但未 finish
        last_fallback_reason = "max_steps_reached_without_finish"

    remaining_issue_details = _issue_details(review_issues, issues_remaining)
    remaining_issue_summary = _format_issue_details(remaining_issue_details)

    pub_head = (
        "修复完成" if review_passed
        else "修复未完成,已交回主图"
    )
    pub_detail = (
        (
            f"处理 {len(issues_resolved)} 个 issue,剩余 {len(issues_remaining)} 个。"
            f"待关注章节: {remaining_issue_summary}"
        )
        if not review_passed else None
    )

    summary = PublicSummary(headline=pub_head[:120], detail=pub_detail)
    snap = budget.snapshot()
    bs = BudgetState(
        steps=snap.steps,
        tool_calls=snap.tool_calls,
        wall_seconds=snap.wall_seconds,
        token_estimate=snap.token_estimate,
        repeated_tool_calls=snap.repeated_tool_calls,
    )

    if last_fallback_reason:
        await emitter.emit_repair_fallback(
            reason=last_fallback_reason,
            fallback_target="repair_fallback_node",
        )

    if not review_passed and remaining_issue_details:
        await emitter.emit_observation_update(
            step_index=max(0, len(steps_audit)),
            tool_name="RepairAgent",
            success=False,
            summary=_format_unresolved_repair_summary(
                fallback_reason=last_fallback_reason,
                remaining_issue_summary=remaining_issue_summary,
            )[:240],
            error_code="REPAIR_UNRESOLVED_BLOCK_ISSUES",
        )

    await emitter.emit_repair_completed(
        issues_resolved=len(issues_resolved),
        issues_remaining=len(issues_remaining),
        rounds_used=rounds_used,
        tool_calls_used=tool_calls_used,
        public_headline=pub_head,
    )

    result = RepairResult(
        review_passed=review_passed,
        issues_resolved=issues_resolved,
        issues_remaining=issues_remaining,
        remaining_issue_details=remaining_issue_details,
        tool_calls_used=tool_calls_used,
        rounds_used=rounds_used,
        modified_section_ids=modified_sections,
        knowledge_evidence=knowledge_evidence,
        confidence=0.0,
        public_summary=summary,
        fallback_reason=last_fallback_reason,
        budget_state=bs,
    )

    # 把审计步骤附在 result 上以便上层节点写 state
    result._audit_steps = steps_audit  # type: ignore[attr-defined]
    result._run_id = run_id  # type: ignore[attr-defined]
    return result


# ── helper builders ─────────────────────────────────────────────────────


def _build_fallback_result(
    *, budget: BudgetTracker, reason: str, loop_count: int,
) -> RepairResult:
    snap = budget.snapshot()
    bs = BudgetState(
        steps=snap.steps,
        tool_calls=snap.tool_calls,
        wall_seconds=snap.wall_seconds,
        token_estimate=snap.token_estimate,
        repeated_tool_calls=snap.repeated_tool_calls,
    )
    return RepairResult(
        review_passed=False,
        issues_resolved=[],
        issues_remaining=[],
        remaining_issue_details=[],
        tool_calls_used=0,
        rounds_used=loop_count,
        modified_section_ids=[],
        knowledge_evidence=[],
        confidence=0.0,
        public_summary=PublicSummary(
            headline="修复阶段跳过",
            detail="Repair Agent 不可用,走 fallback",
        ),
        fallback_reason=reason,
        budget_state=bs,
    )


__all__ = [
    "run_repair",
    "_RepairBudgetLimits",
]


def _task_state_ref(state: Any) -> dict:
    """从 LangGraph State 提取轻量 TaskStateRef（供 bridge 显式传参）。"""
    from app.agent_runtime.context.test_plan_evidence import (
        build_test_plan_repair_state_ref,
    )

    ref: dict = {}
    for key in ("task_goal", "task_type", "locked_sections", "pending_confirmation"):
        val = state.get(key) if isinstance(state, dict) else getattr(state, key, None)
        if val not in (None, "", [], {}):
            ref[key] = val
    if not isinstance(state, dict):
        return ref
    test_plan_content = state.get("test_plan_content")
    test_plan_content = (
        dict(test_plan_content) if isinstance(test_plan_content, dict) else {}
    )
    # Test-plan graph versions have used both nested and direct section state.
    # Normalize at the repair boundary so the required CE evidence section is
    # built from the real generation/review result instead of an empty shell.
    if (
        not test_plan_content.get("generated_sections")
        and isinstance(state.get("generated_sections"), list)
    ):
        test_plan_content["generated_sections"] = state["generated_sections"]
    review_result = state.get("review_result")
    if not isinstance(review_result, dict):
        review_result = {
            key: state[key]
            for key in ("review_issues", "blocking_issues", "block_issues")
            if isinstance(state.get(key), list)
        }
    return build_test_plan_repair_state_ref(
        test_plan_content=test_plan_content,
        review_result=review_result,
        base_state=ref,
    )


def _repair_context_manifest(state_ref: dict[str, Any]) -> dict[str, int]:
    """Return a privacy-safe manifest for repair Context Engine diagnostics."""
    evidence = state_ref.get("context_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    return {
        "generated_content": len(evidence.get("generated_content") or []),
        "review_results": len(evidence.get("review_results") or []),
        "task_state_keys": len(
            [key for key in state_ref if key != "context_evidence"]
        ),
    }


# module-level note (auto-appended):
# Repair 主循环入口(run_repair / run_repair_subgraph)。
# 内部 6 节点: decide / execute_tool / observe / re_review / finish / fallback。
# 关键约束: scope_guard 不绕过(最小修改范围),失败 emit RepairStep(success=False)。
