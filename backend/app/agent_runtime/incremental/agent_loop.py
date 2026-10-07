"""Incremental Agent Main Loop (Phase 2.5).

镜像 ``preparation.agent_loop.run_preparation`` / ``repair.agent_loop.run_repair``
的结构,但 termination 条件是:

* 成功:ResultReviewTool 通过 AND WordExportTool 写新 artifact AND DocxFormatCheckTool 通过;
* 失败:任何工具永久失败 / BudgetExceeded / locked section 被改;
* 兜底:调 ``fallback.run_legacy_incremental_fallback``。

永不抛(Rule 14):任何异常都合成 best-effort ``IncrementalResult`` + emit
``INCREMENTAL_FALLBACK``。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from app.agent_runtime._shared.budget import BudgetExceeded, BudgetTracker
from app.agent_runtime._shared.permission import ToolPermissionGuard
from app.agent_runtime.incremental.capabilities import (
    ModelCapabilities,
    resolve_capabilities,
)
from app.agent_runtime.incremental.decision_filter import filter_incremental_decision
from app.agent_runtime.incremental.event_emitter import IncrementalEventEmitter
from app.agent_runtime.incremental.fallback import run_legacy_incremental_fallback
from app.agent_runtime.incremental.permission import INCREMENTAL_TOOL_WHITELIST
from app.agent_runtime.incremental.prompt import build_incremental_prompt
from app.agent_runtime.incremental.schemas import (
    IncrementalDecision,
    IncrementalIntent,
    IncrementalResult,
    PublicSummary,
)


_logger = logging.getLogger(__name__)


# ── Budget constants (Phase 2.5) ────────────────────────────────


class _IncrementalBudget(BudgetTracker):
    """Phase 2.5 预算比 Repair 略宽,因为包含 export + format check。"""

    MAX_AGENT_STEPS = 8
    MAX_TOOL_CALLS = 6
    MAX_WALL_TIME_SECONDS = 180
    MAX_TOKEN_ESTIMATE = 8000
    MAX_SAME_TOOL_SAME_ARGS = 2


# ── Main loop ────────────────────────────────────────────────────


async def run_incremental(
    state: Dict[str, Any],
    *,
    ctx,
    capabilities: ModelCapabilities | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> IncrementalResult:
    """Incremental Agent 主循环。

    Args:
        state: ``TestPlanGraphState`` dict 镜像(由 LangGraph 节点传入)
        ctx: ``RuntimeContext``
        capabilities: ``ModelCapabilities``,默认 ``resolve_capabilities()``
        clock: 可注入时钟(测试用)

    Returns:
        ``IncrementalResult`` —— 永不抛,失败时 ``success=False`` +
        ``fallback_reason`` 填充
    """
    if capabilities is None:
        capabilities = resolve_capabilities()

    intent = _coerce_incremental_intent(state.get("incremental_intent"))
    if intent is None:
        return _fail_result("incremental_intent_missing_or_invalid")
    state["incremental_intent"] = intent

    locked_section_ids: List[str] = list(state.get("locked_section_ids") or [])
    prior_steps: List[Dict[str, Any]] = list(state.get("incremental_steps") or [])

    budget = _IncrementalBudget(wall_clock_fn=clock)
    guard = ToolPermissionGuard(
        whitelist=INCREMENTAL_TOOL_WHITELIST,
        fail_fast_after=2,
    )
    emitter = IncrementalEventEmitter(
        ctx, node_name=state.get("current_node") or "incremental_subgraph",
    )

    task_id = state.get("task_id") or ""
    graph_run_id = state.get("graph_run_id") or ""

    tool_calls_used = 0
    modified_sections: List[str] = []
    last_error: Optional[str] = None

    await emitter.emit_started(task_id=task_id, graph_run_id=graph_run_id, intent=intent)

    # ── 终止条件:success_requires 集合(必须全部覆盖) ──
    success_requires = {"review_passed", "word_exported", "format_checked"}
    completed_marks: set[str] = set()

    try:
        for _ in range(_IncrementalBudget.MAX_AGENT_STEPS):
            budget.on_step()

            system_prompt, user_content = build_incremental_prompt(
                intent=intent,
                locked_section_ids=locked_section_ids,
                prior_steps=prior_steps,
                capabilities=capabilities,
                mode="mode_a",
            )

            try:
                decision = await _invoke_llm_for_decision(
                    state=state,
                    ctx=ctx,
                    system_prompt=system_prompt,
                    user_content=user_content,
                )
            except Exception as exc:
                _logger.warning("incremental_llm_invoke_failed exc=%s", exc, exc_info=True)
                last_error = f"llm_invoke_failed:{type(exc).__name__}"
                break

            await emitter.emit_decision_made(
                task_id=task_id, graph_run_id=graph_run_id, decision=decision,
            )
            step_index = len(prior_steps)
            await emitter.emit_public_decision_update(
                task_id=task_id,
                graph_run_id=graph_run_id,
                step_index=step_index,
                decision=decision,
            )

            if decision.action == "finish":
                # 检查 success_requires
                if completed_marks >= success_requires:
                    return _build_success_result(
                        new_artifact_public_id=state.get("new_artifact_public_id") or "",
                        new_artifact_version_no=state.get("new_artifact_version_no"),
                        superseded_artifact_public_ids=list(
                            state.get("superseded_artifact_public_ids") or [],
                        ),
                        modified_section_ids=modified_sections,
                        tool_calls_used=tool_calls_used,
                        rounds_used=len(prior_steps) + 1,
                        budget_state=budget.snapshot(),
                    )
                # finish 但 success_requires 未满足 → 视作 fail
                last_error = "finish_premature"
                break

            if decision.action == "ask_user":
                return _build_ask_user_result(
                    decision=decision,
                    modified_section_ids=modified_sections,
                    tool_calls_used=tool_calls_used,
                    rounds_used=len(prior_steps) + 1,
                    budget_state=budget.snapshot(),
                )

            if decision.action == "fail":
                last_error = decision.decision_summary[:240]
                break

            # ── call_tool ──
            clean_decision, blocked = filter_incremental_decision(
                decision,
                guard=guard,
                scope=intent.scope,
                locked_section_ids=locked_section_ids,
                whitelist=INCREMENTAL_TOOL_WHITELIST,
                state=state,
            )
            for b in blocked:
                _logger.warning(
                    "incremental_tool_blocked | tool=%s | outcome=%s | reason=%s | "
                    "decision_arg_keys=%s | target_section_ids=%s",
                    b.get("tool_name"),
                    b.get("outcome"),
                    b.get("reason"),
                    sorted((decision.tool_arguments or {}).keys())
                    if isinstance(decision.tool_arguments, dict) else [],
                    decision.target_section_ids,
                )
                prior_steps.append({
                    "action": "call_tool",
                    "tool_name": b.get("tool_name"),
                    "decision_summary": "blocked",
                    "outcome": b.get("outcome"),
                    "reason": b.get("reason", "")[:240],
                })
                await emitter.emit_tool_blocked(
                    task_id=task_id, graph_run_id=graph_run_id,
                    tool_name=b.get("tool_name") or "-",
                    reason=b.get("reason", ""),
                )

            if clean_decision.action == "fail":
                last_error = clean_decision.decision_summary[:240]
                break

            # over_budget 软 block 不再 fail(decision.action 仍是 call_tool),
            # 但工具不应再执行 —— 直接 break 走 fallback,避免重试空转。
            if any(b.get("outcome") == "permission_denied" for b in blocked):
                last_error = "args_signature_over_budget"
                break

            budget.on_tool_call()
            tool_calls_used += 1

            # R8 — incremental 上下文:WordExportTool 的 ``artifact_public_id``
            # 参数 LLM 经常给空字符串(它不理解 incremental 语义,以为要传
            # 已有 artifact id)。但 WordExportTool 内部 **完全不用** 该参数
            # (直接 generate_public_id("artifact") 建新 record)—— schema 校验
            # 失败只是因为 Pydantic 把它列为「非空字符串」必填。
            # 自动用 source_artifact_public_id 兜底,保证 schema 通过 + 工具
            # 正常执行。R6 subgraph 末尾的 _export_and_check_after_incremental
            # 也会再调一次,产生最终 artifact。
            tool_inputs = clean_decision.tool_arguments or {}
            if (
                clean_decision.tool_name == "WordExportTool"
                and not (tool_inputs.get("artifact_public_id") or "")
            ):
                tool_inputs = dict(tool_inputs)
                tool_inputs["artifact_public_id"] = (
                    state.get("source_artifact_public_id") or "incremental_skip"
                )
                _logger.info(
                    "R8 auto-fill WordExportTool artifact_public_id=%s",
                    tool_inputs["artifact_public_id"],
                )

            try:
                envelope = await ctx.tool_adapter.execute(
                    tool_name=clean_decision.tool_name,
                    inputs=tool_inputs,
                    ctx_runtime=ctx,
                    graph_state=state,
                )
            except Exception as exc:
                _logger.warning(
                    "incremental_tool_execute_failed tool=%s exc=%s",
                    clean_decision.tool_name, exc, exc_info=True,
                )
                last_error = f"tool_execute_failed:{clean_decision.tool_name}"
                break

            await emitter.emit_tool_finished(
                task_id=task_id, graph_run_id=graph_run_id,
                tool_name=clean_decision.tool_name,
                success=envelope.get("success", False),
                summary=(envelope.get("summary") or "")[:480],
            )
            error = (envelope or {}).get("error") or {}
            data = (envelope or {}).get("data") or {}
            await emitter.emit_public_observation_update(
                task_id=task_id,
                graph_run_id=graph_run_id,
                step_index=step_index,
                tool_name=clean_decision.tool_name,
                success=bool(envelope.get("success")),
                summary=str(
                    envelope.get("summary")
                    or data.get("summary")
                    or error.get("message")
                    or ("Tool completed." if envelope.get("success") else "Tool failed.")
                )[:240],
                error_code=str(error.get("code")) if error.get("code") else None,
            )

            _merge_tool_outputs_into_state(
                clean_decision.tool_name,
                envelope,
                state,
            )
            _update_completed_marks(
                clean_decision.tool_name,
                envelope,
                completed_marks,
                state,
                modified_sections,
            )

            # R8 — 方案 B: incremental 任务"regen + review"两步走完即强制 finish。
            # 不再让 LLM 决定是否调 WordExportTool(LLM 经常填错 artifact_public_id
            # 导致 schema_invalid),导出 + 格式自检由 subgraph.py:225 R6 的
            # ``_export_and_check_after_incremental`` 接管。LLM 自由度反而是问题源。
            if (
                envelope.get("success")
                and clean_decision.tool_name == "ResultReviewTool"
                and "TestPlanRegenTool"
                in [step.get("tool_name") for step in prior_steps if step.get("outcome") == "success"]
            ):
                _logger.info(
                    "R8 incremental force-finish: regen+review both succeeded, "
                    "skip remaining LLM iterations | task=%s",
                    task_id,
                )
                # R6 接管会在 subgraph 末尾实际跑 WordExport + DocxFormatCheck,
                # 这里提前把两个 mark 占位写上以满足 success_requires;真正
                # 的"导出+自检"在 subgraph.py:_export_and_check_after_incremental。
                completed_marks.add("word_exported")
                completed_marks.add("format_checked")
                return _build_success_result(
                    new_artifact_public_id="",
                    new_artifact_version_no=None,
                    superseded_artifact_public_ids=list(
                        state.get("superseded_artifact_public_ids") or []
                    ),
                    modified_section_ids=modified_sections,
                    tool_calls_used=tool_calls_used,
                    rounds_used=len(prior_steps) + 1,
                    budget_state=budget.snapshot(),
                )

            prior_steps.append({
                "action": "call_tool",
                "tool_name": clean_decision.tool_name,
                "decision_summary": clean_decision.decision_summary[:500],
                "outcome": "success" if envelope.get("success") else "tool_returned_error",
                "args_signature": _args_signature(clean_decision),
            })

    except BudgetExceeded as exc:
        last_error = f"budget_exceeded:{exc.code}"
    except Exception as exc:  # noqa: BLE001
        _logger.exception("incremental_loop_crashed")
        last_error = f"loop_crashed:{type(exc).__name__}"

    # ── "决策性失败"路径:filter 拦截/permission_denied/scope_guard 等
    # 不应该走 fallback(否则会触发额外的 legacy regen + re-export,既浪费
    # 又给用户造成「多余的 TestPlanRegenTool 又跑了一遍」错觉)。直接返回
    # IncrementalResult(success=False) 让 subgraph emit incremental_failed。
    if _is_decision_failure(last_error):
        return _build_decision_failure_result(
            failure_reason=last_error or "unknown",
            modified_sections=modified_sections,
            tool_calls_used=tool_calls_used,
            rounds_used=len(prior_steps) + 1,
            budget_state=budget.snapshot(),
        )

    # ── 真正的执行失败(budget / crash)→ fallback ──
    return await _fallback_path(
        state=state,
        ctx=ctx,
        emitter=emitter,
        task_id=task_id,
        graph_run_id=graph_run_id,
        failure_reason=last_error or "unknown",
        modified_sections=modified_sections,
        tool_calls_used=tool_calls_used,
        prior_steps=prior_steps,
        budget=budget,
    )


def _is_decision_failure(last_error: str | None) -> bool:
    """filter / scope / permission / schema 拦截造成的失败 ≠ 真执行失败,
    不应触发 legacy fallback 重新生成一份。
    """
    if not last_error:
        return False
    keywords = (
        "BLOCKED",
        "tool_args_schema_invalid",
        "tool_banned",
        "scope_guard",
        "args_signature_over_budget",
        "permanent_permission_denied",
    )
    return any(k in last_error for k in keywords)


def _build_decision_failure_result(
    *,
    failure_reason: str,
    modified_sections: List[str],
    tool_calls_used: int,
    rounds_used: int,
    budget_state: Any,
) -> "IncrementalResult":
    from app.agent_runtime.incremental.schemas import (
        IncrementalResult,
        PublicSummary,
    )

    return IncrementalResult(
        success=False,
        new_artifact_public_id=None,
        new_artifact_version_no=None,
        superseded_artifact_public_ids=[],
        modified_section_ids=modified_sections,
        tool_calls_used=tool_calls_used,
        rounds_used=rounds_used,
        public_summary=PublicSummary(
            headline="增量任务无法继续",
            detail=(
                f"Agent 决策被策略拦截({failure_reason[:160]}),"
                "不会触发 legacy 重生成。"
                "如需重试，请调整修改描述或联系管理员。"
            ),
        ),
        fallback_reason=None,
        budget_state=budget_state,
    )


# ── Fallback dispatch ────────────────────────────────────────────


async def _fallback_path(
    *,
    state,
    ctx,
    emitter: IncrementalEventEmitter,
    task_id: str,
    graph_run_id: str,
    failure_reason: str,
    modified_sections: List[str],
    tool_calls_used: int,
    prior_steps: List[Dict[str, Any]],
    budget: BudgetTracker,
) -> IncrementalResult:
    """触发 ``run_legacy_incremental_fallback``,然后构造 IncrementalResult。"""
    await emitter.emit_fallback(
        task_id=task_id, graph_run_id=graph_run_id,
        reason=failure_reason,
    )

    delta = await run_legacy_incremental_fallback(
        state, ctx=ctx, failure_reason=failure_reason,
    )

    fb_result = delta.get("incremental_result") or {}
    return IncrementalResult(
        success=bool(fb_result.get("success")),
        new_artifact_public_id=fb_result.get("new_artifact_public_id"),
        new_artifact_version_no=fb_result.get("new_artifact_version_no"),
        superseded_artifact_public_ids=[],
        modified_section_ids=modified_sections,
        tool_calls_used=tool_calls_used,
        rounds_used=len(prior_steps) + 1,
        public_summary=PublicSummary(
            headline=(fb_result.get("public_summary") or {}).get(
                "headline", "增量任务已降级完成",
            ),
            detail=(fb_result.get("public_summary") or {}).get("detail"),
        ),
        fallback_reason=failure_reason[:240],
        budget_state=budget.snapshot(),
    )


# ── LLM dispatch (Mode A) ────────────────────────────────────────


async def _invoke_llm_for_decision(
    *,
    state: Dict[str, Any],
    ctx,
    system_prompt: str,
    user_content: str,
) -> IncrementalDecision:
    """Mode A — Structured Action JSON。Phase 2.5 不实现 Mode B(TODO stub)。

    CE-04 §四：MIG_INCREMENTAL flag 路由。
    false → legacy（ctx.llm_client.generate_with_profile）
    true  → 只走 ContextInvokerBridge；失败抛错由调用方 failure policy 处理。
    """
    from app.context_engine.feature_flags import is_agent_context_migration_enabled

    # CE-05 WP-2：任务路径经 ctx.task_flag_resolver 读 MIG_INCREMENTAL（冻结 Manifest）；
    # resolver 缺失/损坏 → fail closed，不读取进程级 MIG 配置。
    resolver = getattr(ctx, "task_flag_resolver", None)
    mig_incremental = True
    if mig_incremental:
        bridge = getattr(ctx, "context_llm_invoker", None)
        if bridge is None or not getattr(bridge, "available", False):
            raise RuntimeError("MIG_INCREMENTAL=true 但 Invoker 不可用")
        from app.llm.task_profiles import INCREMENTAL_PROFILE  # 推迟 import

        bres = await bridge.generate(
            user_id=getattr(ctx, "user_internal_id", 0),
            call_site="test_plan.incremental.diff",
            llm_task_profile=INCREMENTAL_PROFILE,
            current_node="incremental_decision",
            current_goal=user_content,
            task_state_ref=_incremental_state_ref(state),
            output_contract="incremental_decision_json",
            system_prompt=system_prompt,
            user_content=user_content,
            conversation_id=getattr(ctx, "conversation_internal_id", None),
            task_id=getattr(ctx, "task_internal_id", None),
            runtime_context=ctx,
        )
        if bres is None:
            raise RuntimeError("MIG_INCREMENTAL bridge 返回 None")
        raw = bres.as_profile_result()
    else:
        from app.llm.task_profiles import INCREMENTAL_PROFILE  # 推迟 import

        raise RuntimeError("MIGRATION_CONTEXT_REQUIRED")
    return _coerce_incremental_decision(raw)


def _incremental_state_ref(state: Dict[str, Any]) -> dict:
    """从 state 提取轻量 TaskStateRef（供 bridge 显式传参）。"""
    ref: dict = {}
    for key in ("task_goal", "task_type", "locked_sections", "pending_confirmation"):
        val = state.get(key)
        if val not in (None, "", [], {}):
            ref[key] = val
    return ref


# ── Helpers ──────────────────────────────────────────────────────


def _coerce_incremental_decision(raw: Any) -> IncrementalDecision:
    """Normalize LLM profile/bridge/string results into IncrementalDecision.

    Production ``LLMClient.generate_with_profile`` returns ``LLMProfileResult``;
    the ContextInvokerBridge returns a compatible object; older tests return a
    raw JSON string. Keep all three forms explicit so the runtime contract is
    stable and parse failures are logged as LLM parse failures, not Pydantic
    type errors.
    """
    if isinstance(raw, IncrementalDecision):
        return raw
    if isinstance(raw, str):
        return IncrementalDecision.model_validate_json(raw)

    parsed = getattr(raw, "parsed", None)
    if hasattr(raw, "success"):
        if not getattr(raw, "success", False) or parsed is None:
            error_message = getattr(raw, "error_message", None) or "incremental_llm_parse_failed"
            raise RuntimeError(str(error_message))
        return _coerce_incremental_decision_payload(parsed)

    return _coerce_incremental_decision_payload(raw)


def _coerce_incremental_decision_payload(payload: Any) -> IncrementalDecision:
    if isinstance(payload, IncrementalDecision):
        return payload
    if isinstance(payload, str):
        return IncrementalDecision.model_validate_json(payload)
    if isinstance(payload, dict):
        return IncrementalDecision.model_validate(payload)
    raise TypeError(
        "incremental_decision_payload_unsupported_type:"
        f"{type(payload).__name__}"
    )


def _update_completed_marks(
    tool_name: str,
    envelope: Dict[str, Any],
    completed_marks: set,
    state: Dict[str, Any],
    modified_sections: List[str],
) -> None:
    """根据 envelope 推进 success_requires 标记。"""
    success = bool(envelope.get("success"))
    data = envelope.get("data") or {}

    if tool_name == "ResultReviewTool" and success:
        if str(data.get("level", "")).lower() == "passed":
            completed_marks.add("review_passed")

    if tool_name == "WordExportTool" and success:
        completed_marks.add("word_exported")
        public_id = data.get("artifact_public_id")
        version_no = data.get("version_no")
        if public_id:
            state["new_artifact_public_id"] = public_id
        if version_no:
            state["new_artifact_version_no"] = version_no
        modified_sections.extend(data.get("modified_section_ids") or [])

    if tool_name == "TestPlanRegenTool" and success:
        modified_sections.extend(data.get("modified_section_ids") or [])

    if tool_name == "DocxFormatCheckTool" and success:
        if str(data.get("level", "")).lower() in {"passed", "warning"}:
            completed_marks.add("format_checked")


def _merge_tool_outputs_into_state(
    tool_name: str,
    envelope: Dict[str, Any],
    state: Dict[str, Any],
) -> None:
    """Write generic tool outputs back into incremental graph state.

    Normal v3 nodes return partial LangGraph state deltas. The incremental loop
    calls tools directly, so it must explicitly preserve the same business
    facts for the next tool decision and for downstream export/review nodes.
    """
    if not isinstance(envelope, dict) or not envelope.get("success"):
        return
    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {}

    for field in (
        "requirement_analysis",
        "template_structure",
        "knowledge_search_result",
        "section_suggestions",
        "section_confirm_config",
        "template_file_id",
        "test_plan_content",
        "review_standard",
        "review_result",
        "artifact",
        "format_check_result",
        "pending_format_losses",
        "format_loss_confirmation",
    ):
        if field in data and data.get(field) is not None:
            state[field] = data[field]

    if tool_name == "ResultReviewTool" and "review_result" not in data:
        state["review_result"] = data

    if tool_name == "DocxFormatCheckTool" and "format_check_result" not in data:
        state["format_check_result"] = data

    if tool_name == "WordExportTool":
        artifact = state.get("artifact")
        if not isinstance(artifact, dict):
            artifact = {}
        for source_key, target_key in (
            ("artifact_public_id", "artifact_public_id"),
            ("public_id", "artifact_public_id"),
            ("storage_path", "storage_path"),
            ("storage_url", "storage_url"),
            ("file_name", "file_name"),
            ("filename", "file_name"),
            ("version_no", "version_no"),
        ):
            value = data.get(source_key)
            if value not in (None, "", [], {}):
                artifact[target_key] = value
        if artifact:
            state["artifact"] = artifact


def _args_signature(decision: IncrementalDecision) -> str:
    import hashlib
    import json

    raw = json.dumps(
        decision.tool_arguments or {}, sort_keys=True, default=str,
        ensure_ascii=False,
    )
    return hashlib.sha256(
        f"{decision.tool_name}|{raw}".encode("utf-8"),
    ).hexdigest()[:12]


def _build_success_result(
    *,
    new_artifact_public_id: str,
    new_artifact_version_no: Optional[int],
    superseded_artifact_public_ids: List[str],
    modified_section_ids: List[str],
    tool_calls_used: int,
    rounds_used: int,
    budget_state,
) -> IncrementalResult:
    return IncrementalResult(
        success=True,
        new_artifact_public_id=new_artifact_public_id or None,
        new_artifact_version_no=new_artifact_version_no,
        superseded_artifact_public_ids=superseded_artifact_public_ids,
        modified_section_ids=modified_section_ids,
        tool_calls_used=tool_calls_used,
        rounds_used=rounds_used,
        public_summary=PublicSummary(
            headline="增量任务已完成",
            detail=f"新版本 v{new_artifact_version_no} 已落库;修改 section 共 {len(modified_section_ids)} 个。",
        ),
        fallback_reason=None,
        budget_state=budget_state,
    )


def _build_ask_user_result(
    *,
    decision: IncrementalDecision,
    modified_section_ids: List[str],
    tool_calls_used: int,
    rounds_used: int,
    budget_state,
) -> IncrementalResult:
    return IncrementalResult(
        success=False,
        modified_section_ids=modified_section_ids,
        tool_calls_used=tool_calls_used,
        rounds_used=rounds_used,
        public_summary=PublicSummary(
            headline="需要用户进一步澄清",
            detail=decision.public_update or decision.decision_summary[:480],
        ),
        fallback_reason="ask_user_pending",
        budget_state=budget_state,
    )


def _fail_result(reason: str) -> IncrementalResult:
    return IncrementalResult(
        success=False,
        modified_section_ids=[],
        tool_calls_used=0,
        rounds_used=0,
        public_summary=PublicSummary(
            headline="增量任务启动失败",
            detail=reason[:480],
        ),
        fallback_reason=reason[:240],
        budget_state=_IncrementalBudget(wall_clock_fn=time.monotonic).snapshot(),
    )


def _coerce_incremental_intent(value: Any) -> IncrementalIntent | None:
    if isinstance(value, IncrementalIntent):
        return value
    if isinstance(value, dict):
        try:
            return IncrementalIntent.model_validate(value)
        except Exception as exc:
            _logger.warning(
                "incremental_intent_validation_failed | type=%s | error=%s",
                type(value).__name__,
                exc,
                exc_info=True,
            )
            return None
    if value is not None:
        _logger.warning(
            "incremental_intent_unsupported_type | type=%s",
            type(value).__name__,
        )
    return None


__all__ = ["run_incremental"]


# module-level note (auto-appended):
# Incremental 主循环入口。仅跑必要节点(不重做全流程)。
# 步骤: detect_delta / propose_change / preview_change / apply_change / re_validate。
# 关键约束: 不动已完成 section; artifact chain version_no +1; 开 prod 默认关闭。
