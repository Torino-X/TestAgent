"""Preparation Agent 主循环 (Phase 2.3 — Mode A only, Mode B TODO)。

设计原则:
* 永不抛 — 失败时合成 best-effort PreparationResult + emit PREPARATION_FALLBACK
* 单步决策 = (action, tool_name, tool_arguments, decision_summary, public_update)
* 工具调用一律走 TestAgentToolAdapter.execute() (Phase 2.1 已有的白名单 + 节奏 + 信封)
* 完整审计链写入 state.preparation_steps;仅存 decision_summary ≤500 + 12 字符
  args_signature,绝无 raw LLM 文本 (Rule 11)
* 单一终止: budget exhaust / permanent permission deny / loop_error / decision.finish
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.agent_runtime._shared.public_narrative import AgentPublicUpdateDraft
from app.agent_runtime._shared.narrative_governance.settings_service import (
    is_tool_card_narrative_generation_enabled,
)
from app.agent_runtime.feature_flags import get_feature_flags
from app.agent_runtime.narrative_composer.composer import NarrativeComposer
from app.agent_runtime.narrative_composer.context_builders import (
    get_tool_context_builder,
)
from app.agent_runtime.preparation.budget import BudgetExceeded, BudgetTracker
from app.agent_runtime.preparation.capabilities import (
    ModelCapabilities,
    resolve_capabilities,
)
from app.agent_runtime.preparation.event_emitter import PreparationEventEmitter
from app.agent_runtime.preparation.permission import (
    PermanentPermissionDenied,
    ToolPermissionDenied,
    ToolPermissionGuard,
)
from app.agent_runtime.preparation.prompt import (
    args_signature,
    build_preparation_prompt,
)
from app.agent_runtime.preparation.schemas import (
    AgentDecision,
    BudgetState,
    ClarificationGap,
    KnowledgeEvidence,
    PreparationResult,
    PublicSummary,
)
from app.agent_runtime.preparation.tool_filter import filter_decision_tool_calls
from app.llm.task_profiles import PREPARATION_PROFILE

logger = logging.getLogger(__name__)


# ── 客户端协议 (避免 agent_loop 直接 import LLMClient) ──────────────────


class _LLMClientLike:
    """Protocol-like 标记 — 任何有 ``generate_with_profile`` 方法的对象都能用。"""

    async def generate_with_profile(
        self,
        profile,
        user_content,
        *,
        parser=None,
        system_prompt_override=None,
    ):
        ...


class _ToolAdapterLike:
    """Protocol-like 标记 — TestAgentToolAdapter 兼容。"""

    async def execute(self, *, tool_name: str, inputs: Dict[str, Any],
                      ctx_runtime, attempt: int = 1,
                      retry_context=None) -> Dict[str, Any]:
        ...


# ── Mode B TODO stub (test_prep_native_mode_b_deferred) ─────────────────


class ModeBNotImplemented(NotImplementedError):
    """Mode B native tool-calling 推迟到 Phase 2.4+。"""


def _maybe_mode_b(capabilities: ModelCapabilities) -> str:
    """决定当前运行使用 Mode A 还是 Mode B。

    Phase 2.3 仅 Mode A。Mode B 仅在测试显式 override capabilities.native_tool_calling=True
    时被调用,且抛 ModeBNotImplemented (test_prep_native_mode_b_deferred 验证)。
    """
    if capabilities.native_tool_calling:
        raise ModeBNotImplemented(
            "Mode B (native tool-calling) deferred to Phase 2.4+; "
            "LLMClient currently has no generate_with_tools() API."
        )
    return "mode_a"


# ── 主循环 ────────────────────────────────────────────────────────────────


async def run_preparation(
    state: Dict[str, Any],
    *,
    llm_client: _LLMClientLike,
    tool_adapter: _ToolAdapterLike,
    ctx: Any,
    capabilities: Optional[ModelCapabilities] = None,
    clock: Callable[[], float] = time.monotonic,
    parser: Any = None,
) -> PreparationResult:
    """Preparation Agent 入口。永不抛。

    Args:
        state: TestPlanGraphState dict (in-process slice). We only read:
            user_prompt / requirement_summary / template_summary /
            knowledge_search_result; never mutate (subgraph returns new state).
        llm_client: 满足 ``generate_with_profile(profile, content)`` 协议
        tool_adapter: TestAgentToolAdapter 实例
        ctx: RuntimeContext (or compatible duck-typed)
        capabilities: 默认 ``resolve_capabilities()``
        clock: 单调时间源 (测试可注入)
        parser: 可选预构建 ParserAdapter,省略则 PREPARATION_PROFILE.parser 决定

    Returns:
        PreparationResult — 即使中途 fallback 也返回结构化结果
    """
    caps = capabilities or resolve_capabilities()
    mode = _maybe_mode_b(caps)

    budget = BudgetTracker(wall_clock_fn=clock)
    guard = ToolPermissionGuard()
    events = PreparationEventEmitter(ctx)

    cap_summary = f"{caps.provider_label} native_tool_calling={caps.native_tool_calling} mode={mode}"
    await events.emit_preparation_started(cap_summary)

    steps_audit: List[Dict[str, Any]] = []
    evidence: List[KnowledgeEvidence] = []
    queries: List[str] = []
    requirement_gaps: List[Any] = []
    user_questions: List[Any] = []
    constraints: List[str] = []
    knowledge_search_used = False
    fallback_reason: Optional[str] = None
    information_sufficient = False

    try:
        for _ in range(BudgetTracker.MAX_AGENT_STEPS + 1):  # +1 safety net
            budget.on_step()

            # ── 1. LLM 单步决策 ─────────────────────────────────────
            system_prompt, user_content = build_preparation_prompt(
                user_prompt=str(state.get("user_prompt") or ""),
                requirement_summary=str(state.get("requirement_summary") or ""),
                template_summary=str(state.get("template_summary") or ""),
                existing_kb_result=_preparation_evidence_context(state),
                prior_steps=steps_audit,
                capabilities=caps,
                mode=mode,
            )
            decision = await _llm_decide(
                llm_client=llm_client,
                system_prompt=system_prompt,
                user_content=user_content,
                parser=parser,
                ctx=ctx,
                task_state_ref=_prep_state_ref(state),
            )
            decision = _promote_explicit_critical_finish(state, decision)
            decision = _accept_submitted_clarification(state, decision)

            step_index = len(steps_audit)
            # Phase 2.9B: decision_id 用 task_internal_id(无 task_id 属性)。
            _task_id = str(getattr(ctx, "task_internal_id", ""))
            # Phase 2.9B.3: 只允许把模型生成的 decision_update / observation_update
            # 作为动态叙事。decision_summary 是内部字段(可能含 Schema 错误),绝不
            # 直接作为 public narrative;两者都缺时由 emitter 生成确定性公开回退。
            await events.emit_decision_update(
                decision_id=f"{_task_id}:preparation:{step_index}",
                step_index=step_index,
                action=decision.action,
                tool_name=decision.tool_name,
                public_update=_decision_public_update(decision, state),
                failure_category="schema_validation_failed" if decision.action == "fail" else None,
            )
            _audit_step(steps_audit, decision, tool_name=decision.tool_name or "")

            # ── 2. 终止 action ──────────────────────────────────────
            if decision.action == "finish":
                information_sufficient = True
                break

            if decision.action == "ask_user":
                # The LLM owns the gap judgment.  The runtime only preserves
                # its bounded, structured result for retrieval, the durable
                # clarification card, and final-context auditing.
                requirement_gaps.extend(
                    gap.model_dump(mode="python")
                    for gap in decision.clarification_gaps
                )
                user_questions.extend(_gaps_to_questions(decision))
                information_sufficient = False
                break

            if decision.action == "fail":
                await events.emit_preparation_fallback(reason=decision.decision_summary)
                fallback_reason = decision.decision_summary[:240]
                break

            # ── 3. call_tool ────────────────────────────────────────
            assert decision.action == "call_tool"
            clean, blocked = filter_decision_tool_calls(decision, guard)

            if blocked:
                _audit_blocked(steps_audit, decision, blocked)
                # 永久拒绝 → fail-fast
                if guard.is_permanently_denied() or isinstance(
                    None, PermanentPermissionDenied  # noqa: ARG — placeholder
                ):
                    # 实际由 _audit_blocked 之前的 authorize() 抛 PermanentPermissionDenied 路径覆盖
                    pass
                # 已被 filter 转化为 action=fail 时,直接 break
                if clean.action == "fail":
                    await events.emit_preparation_fallback(reason="permission_denied")
                    fallback_reason = "tool_permission_denied"
                    break
                continue

            # ── 4. 调工具 ──────────────────────────────────────────
            tool_name = clean.tool_name or ""
            tool_inputs = clean.tool_arguments or {}
            sig = args_signature(tool_inputs)

            # The test-plan graph owns the bounded dual-source retrieval stage.
            # Do not execute only the Company RAG inline here: that would emit a
            # partial result before Project RAG has run, and a disabled Company
            # RAG could otherwise short-circuit preparation entirely.
            if (
                tool_name == "KnowledgeSearchTool"
                and bool(state.get("dual_rag_orchestration"))
            ):
                query = str(tool_inputs.get("query") or "").strip()
                if query:
                    queries.append(query)
                    constraints.append("knowledge_retrieval_requested:" + query[:240])
                break

            budget.on_tool_call()

            try:
                envelope = await tool_adapter.execute(
                    tool_name=tool_name,
                    inputs=tool_inputs,
                    ctx_runtime=ctx,
                )
            except (ToolPermissionDenied, PermanentPermissionDenied) as exc:
                logger.warning("prep tool permission denied: %s", exc)
                await events.emit_preparation_fallback(reason="permission_denied")
                fallback_reason = "tool_permission_denied"
                break
            except Exception as exc:  # noqa: BLE001 — 永不外泄
                logger.warning("prep tool exception %s: %s", tool_name, exc)
                await events.emit_preparation_fallback(reason="tool_error")
                fallback_reason = "tool_error"
                break

            # ── 5. 解析 envelope ────────────────────────────────────
            if get_feature_flags().phase29b_tool_narrative_enabled:
                await _compose_dynamic_tool_narrative(
                    state=state,
                    tool_name=tool_name,
                    tool_inputs=tool_inputs,
                    envelope=envelope,
                    tool_adapter=tool_adapter,
                    llm_client=llm_client,
                    ctx=ctx,
                )

            err = envelope.get("error") or {}
            await events.emit_observation_update(
                decision_id=f"{_task_id}:preparation:observation:{step_index}",
                step_index=step_index,
                observation={
                    "tool_name": tool_name,
                    "success": bool(envelope.get("success", False)),
                    "error_code": str(err.get("code")) if err.get("code") else None,
                    "result_excerpt": str(
                        envelope.get("summary")
                        or err.get("message")
                        or "工具执行完成。"
                    )[:240],
                    "retryable": err.get("recoverable"),
                },
                public_update=decision.observation_update,
            )

            queries.append(tool_inputs.get("query") or "")
            knowledge_search_used = True

            if not envelope.get("success", False):
                err = envelope.get("error") or {}
                if err.get("recoverable"):
                    await events.emit_preparation_fallback(reason="tool_recoverable_failure")
                    fallback_reason = "tool_recoverable_failure"
                else:
                    await events.emit_preparation_fallback(reason="tool_failure")
                    fallback_reason = "tool_failure"
                break

            data = envelope.get("data") or {}
            if data.get("disabled_by_config"):
                await events.emit_knowledge_skipped(
                    reason=str(data.get("skip_reason") or "disabled_by_config")
                )
                # Company RAG is optional, not equivalent to evidence being
                # sufficient.  Let the graph perform Project RAG (when this
                # conversation belongs to a project) and re-enter the LLM for
                # a real PreparationAgent observation/decision.
                constraints.append(
                    "company_rag_skipped:"
                    + str(data.get("skip_reason") or "disabled_by_config")[:160]
                )
                information_sufficient = False
                break

            chunks = data.get("chunks") or []
            if not chunks:
                await events.emit_knowledge_insufficient(queries_attempted=len(queries))
                # 信息不足 — 记录缺口 + 让 LLM 在下轮决定是否换 query 重试
                constraints.append(
                    "knowledge_returned_zero_chunks_after_%d_query" % len(queries)
                )
                # 不 break — 留给 LLM 决定是否精炼或 finish
                continue

            for chunk in chunks[:6]:  # 截 6 条防膨胀
                try:
                    evidence.append(KnowledgeEvidence(
                        query=str(tool_inputs.get("query") or "")[:240],
                        snippet=str(chunk.get("text") or chunk.get("content") or "")[:400],
                        source=str(chunk.get("title") or chunk.get("source") or "")[:120] or None,
                    ))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("evidence parse skip: %s", exc)
            # LLM 看到 evidence 后,在下轮决定 finish 或再调;loop 自然终止

    except BudgetExceeded as exc:
        await events.emit_preparation_budget_exhausted(code=exc.code)
        fallback_reason = f"budget_exhausted:{exc.code}"
    except Exception as exc:  # noqa: BLE001 — 永不外泄 (Rule 14)
        logger.exception("prep loop crash")
        await events.emit_preparation_fallback(reason="loop_error")
        fallback_reason = "loop_error"

    # ── 兜底 finish (loop 自然结束但未声明 finish) ─────────────────────
    result = _build_result(
        information_sufficient=information_sufficient,
        knowledge_search_used=knowledge_search_used,
        queries=queries,
        evidence=evidence,
        requirement_gaps=requirement_gaps,
        user_questions=user_questions,
        constraints=constraints,
        fallback_reason=fallback_reason,
        budget_snapshot=budget.snapshot(),
        steps_audit=steps_audit,
    )
    # Emit PREPARATION_COMPLETED on the success path (no fallback / no budget exhaust).
    # Sanitized payload only — see event_emitter.prepare_preparation_completed.
    await events.emit_preparation_completed(result)
    return result


# ── helpers ──────────────────────────────────────────────────────────────


_UNRESOLVED_MARKERS = (
    "待确认",
    "未确定",
    "不明确",
    "待补充",
    "尚未确定",
    "暂未确定",
)

_EXPLICIT_CRITICAL_GROUPS = (
    (
        "scope_and_capacity",
        ("适用范围", "使用范围", "服务范围", "访客范围", "预约容量", "容量", "预约时段"),
        "请确认适用对象、预约容量与可预约时段；这些会改变测试覆盖边界、边界值和并发场景。",
    ),
    (
        "approval_and_verification",
        ("审批", "超时", "取消", "爽约", "核验", "身份验证", "身份核验"),
        "请确认审批、超时/取消及到访核验规则；这些决定状态流转、权限和异常测试用例。",
    ),
    (
        "privacy_and_acceptance",
        ("隐私", "数据权限", "数据保留", "删除", "验收", "准出", "上线标准"),
        "请确认数据隐私/权限处理及验收、准出标准；这些决定安全合规测试和结果判定口径。",
    ),
)


def _promote_explicit_critical_finish(
    state: Dict[str, Any], decision: AgentDecision
) -> AgentDecision:
    """Prevent an explicitly unresolved business decision from silently finishing.

    Semantic gap discovery remains LLM-owned.  This narrow safety boundary only
    handles facts that the current requirement itself explicitly labels as
    unresolved; those cannot be converted into an LLM-invented conservative
    assumption.  A task-scoped user answer (including an explicit conservative
    choice) resolves the corresponding guard item on the next preparation pass.
    """
    if decision.action != "finish":
        return decision

    gaps = _unanswered_explicit_critical_gaps(state)
    if not gaps:
        return decision

    bullets = "\n".join(f"- {gap.description}" for gap in gaps)
    return AgentDecision(
        action="ask_user",
        action_reason="需求已明确标注关键业务决策未定，不能由系统自行采用保守范围。",
        decision_summary="当前需求明确存在未决关键业务规则，需要用户选择或补充后才能确定测试结论。",
        public_update="需求中已标注关键业务规则待确认，需要补充后再进入章节处理。",
        clarification_gaps=gaps,
        decision_update={
            "headline": "需要确认关键业务规则",
            "narrative_text": (
                "需求文档已明确标出以下未决业务规则。它们不能仅以“待确认”写入方案，"
                "也不能由系统自行采用保守范围：\n" + bullets
            ),
            "summary": "关键业务规则尚未确定，需要用户选择或补充。",
            "impact": "这些决定会改变测试范围、流程用例或验收结论。",
            "next_action": "展示补充卡片，等待你明确选择或填写。",
            "details": [gap.description for gap in gaps],
        },
        expected_result="记录明确未决的关键业务规则并进入任务级补充流程。",
        confidence=1.0,
    )


def _accept_submitted_clarification(
    state: Dict[str, Any], decision: AgentDecision
) -> AgentDecision:
    """Keep one accepted clarification round authoritative for this task.

    The card requires an answer for every displayed gap, or an explicit allowed
    conservative choice.  Reopening clarification immediately after submission
    only replaces one arbitrary question set with another and traps the task in
    a confirmation loop.  Any remaining non-baseline uncertainty is tracked as
    a risk in the generated plan instead of creating another card round.
    """
    if decision.action != "ask_user":
        return decision

    answers = state.get("clarification_answers")
    if not isinstance(answers, dict):
        return decision
    response_map = answers.get("answers")
    conservative_ids = answers.get("conservative_gap_ids")
    has_answer = isinstance(response_map, dict) and any(
        str(value).strip() for value in response_map.values()
    )
    has_conservative_choice = isinstance(conservative_ids, list) and bool(
        conservative_ids
    )
    if not (has_answer or has_conservative_choice):
        return decision

    return AgentDecision(
        action="finish",
        action_reason="本次任务的关键补充已由用户确认，按确认内容继续生成。",
        decision_summary="已采用本次用户补充作为测试范围与验收依据，不再重复发起补充确认。",
        public_update="已收到并采用你的补充信息，后续将据此生成测试方案。",
        clarification_gaps=[],
        decision_update={
            "headline": "已采用你的补充信息",
            "narrative_text": (
                "### 观察\n"
                "1. 你已完成本次关键业务规则的补充，答案将作为当前任务的测试依据。\n"
                "2. 新识别的非基线细节将记录为方案风险或后续跟踪项，不再重复要求填写。\n\n"
                "### 下一步\n"
                "1. 基于已确认的范围、规则与验收口径，进入章节处理和测试方案生成。"
            ),
            "summary": "已采用用户补充信息。",
            "impact": "测试范围和验收判断将以本次确认内容为准。",
            "next_action": "进入章节处理和测试方案生成。",
            "details": [],
        },
        expected_result="使用已确认的任务级规则继续生成测试方案，不再次创建补充卡片。",
        confidence=1.0,
    )


def _unanswered_explicit_critical_gaps(state: Dict[str, Any]) -> list[ClarificationGap]:
    """Return grouped critical decisions explicitly left open by the requirement."""
    requirement = str(state.get("requirement_summary") or "").strip()
    if not requirement:
        return []

    answers = state.get("clarification_answers")
    answers = answers if isinstance(answers, dict) else {}
    resolved_ids = set()
    response_map = answers.get("answers")
    if isinstance(response_map, dict):
        resolved_ids.update(str(key) for key, value in response_map.items() if str(value).strip())
    conservative_ids = answers.get("conservative_gap_ids")
    if isinstance(conservative_ids, list):
        resolved_ids.update(str(item) for item in conservative_ids)

    segments = [
        segment.strip()
        for segment in re.split(r"[。；;！!\n]+", requirement)
        if segment.strip()
    ]
    gaps: list[ClarificationGap] = []
    for field, terms, description in _EXPLICIT_CRITICAL_GROUPS:
        if field in resolved_ids:
            continue
        if any(
            any(term in segment for term in terms)
            and any(marker in segment for marker in _UNRESOLVED_MARKERS)
            for segment in segments
        ):
            gaps.append(
                ClarificationGap(field=field, description=description, severity="high")
            )
    return gaps


def _preparation_evidence_context(state: Dict[str, Any]) -> Dict[str, Any] | None:
    """Expose frozen company/project evidence and task-only answers to re-evaluation."""
    base = state.get("knowledge_search_result")
    context = dict(base) if isinstance(base, dict) else {}
    bundle = state.get("retrieval_evidence_bundle")
    if isinstance(bundle, dict):
        chunks: list[dict[str, Any]] = list(context.get("chunks") or [])[:3]
        for source_key, label in (("project_rag", "项目资料"), ("company_rag", "公司规则")):
            source = bundle.get(source_key)
            if not isinstance(source, dict):
                continue
            for item in list(source.get("hits") or [])[:3]:
                if not isinstance(item, dict):
                    continue
                chunks.append({
                    "title": f"{label}: {item.get('title') or item.get('source') or 'evidence'}",
                    "text": str(item.get("content") or item.get("text") or ""),
                })
        context["chunks"] = chunks[:6]
    answers = state.get("clarification_answers")
    if isinstance(answers, dict):
        context["clarification_answers"] = answers
    return context or None


async def _compose_dynamic_tool_narrative(
    *,
    state: Dict[str, Any],
    tool_name: str,
    tool_inputs: Dict[str, Any],
    envelope: Dict[str, Any],
    tool_adapter: Any,
    llm_client: Any,
    ctx: Any,
) -> None:
    """Generate a Tool Narrative for tools invoked inside PreparationAgent.

    Main graph tool nodes pass through ``tool_narrative_barrier``. Dynamic
    agent loops call tools inline, so they need the same synchronous ordering
    here: terminal tool event, then narrative, then the next agent step.
    """
    if (
        ctx is None
        or getattr(ctx, "event_sink", None) is None
        or not await is_tool_card_narrative_generation_enabled(ctx)
    ):
        return
    bridge = getattr(ctx, "context_llm_invoker", None)
    if bridge is None or not getattr(bridge, "available", False):
        return
    llm_client = bridge.bind(
        user_id=int(getattr(ctx, "user_internal_id", 0) or 0),
        call_site="test_plan.tool_narrative",
        task_id=getattr(ctx, "task_internal_id", None),
        runtime_context=ctx,
    )

    tool_call_id = str(envelope.get("tool_call_id") or "").strip()
    if not tool_call_id:
        logger.warning(
            "dynamic tool narrative skipped: missing tool_call_id | tool=%s",
            tool_name,
        )
        return

    source_event_id = ""
    try:
        last_terminal = getattr(tool_adapter, "last_terminal_event_id", None)
        if callable(last_terminal):
            source_event_id = str(last_terminal() or "")
    except Exception as exc:  # noqa: BLE001
        logger.debug("dynamic tool narrative terminal event lookup failed: %s", exc)

    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
    narrative_state = dict(state or {})
    if tool_name == "KnowledgeSearchTool":
        kb_result = dict(data or {})
        kb_result.setdefault("query", str(tool_inputs.get("query") or ""))
        if not envelope.get("success", False):
            kb_result.setdefault(
                "skip_reason",
                str(error.get("code") or error.get("message") or "tool_failed"),
            )
            kb_result.setdefault("hit_count", 0)
            kb_result.setdefault("used_count", 0)
        narrative_state["knowledge_search_result"] = kb_result

    terminal_status = "success" if envelope.get("success", False) else "failed"
    if isinstance(data, dict) and (data.get("disabled_by_config") or data.get("skip_reason")):
        terminal_status = "skipped"

    context = get_tool_context_builder(tool_name).build(
        graph_state=narrative_state,
        tool_call_id=tool_call_id,
        source_event_id=source_event_id,
        attempt=int(envelope.get("attempt") or 1),
        terminal_status=terminal_status,
        duration_ms=envelope.get("duration_ms"),
        continuation_route="preparation_agent_continue",
    )
    composer = NarrativeComposer(
        llm_client,
        ctx.event_sink,
        deterministic_fallback=AgentPublicUpdateDraft(
            headline="工具执行结果已记录",
            summary=str(envelope.get("summary") or "工具调用已返回。")[:200],
            impact="系统将根据当前结果继续推进任务。",
            next_action="继续执行准备阶段后续流程。",
        ),
        timeout_seconds=get_feature_flags().phase29b_narrative_timeout_seconds,
        repair_attempts=get_feature_flags().phase29b_narrative_repair_attempts,
    )
    try:
        await composer.compose_tool_narrative(
            task_internal_id=ctx.task_internal_id,
            graph_run_id=f"run-{ctx.task_internal_id}",
            context=context,
            narrative_id=f"nar_{uuid.uuid4().hex[:12]}",
            generation_id=f"gen_{uuid.uuid4().hex[:12]}",
            generation_no=1,
            event_prefix="tool",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dynamic tool narrative failed without blocking task | tool=%s | err=%s",
            tool_name,
            exc,
        )


async def _llm_decide(
    *,
    llm_client: _LLMClientLike,
    system_prompt: str,
    user_content: str,
    parser: Any,
    ctx: Any = None,
    task_state_ref: dict | None = None,
) -> AgentDecision:
    """调 LLM 并解析为 AgentDecision。失败一律返回 action=fail 决策,绝不抛。

    CE-04 §四：MIG_PREPARATION flag 路由。
    false → 明确走 legacy（llm_client.generate_with_profile）
    true  → 只走 ContextInvokerBridge；失败执行本函数 fail 决策策略，
            不静默回退 legacy LLMClient。
    """
    # Use a fresh system_prompt context (PREPARATION_PROFILE.system_prompt is empty;
    # caller supplies system_prompt via prompt arg).
    from app.context_engine.feature_flags import is_agent_context_migration_enabled

    # CE-05 WP-2：任务路径经 ctx.task_flag_resolver 读 MIG_PREPARATION（冻结 Manifest）；
    # resolver 缺失/损坏 → fail closed，不读取进程级 MIG 配置。
    resolver = getattr(ctx, "task_flag_resolver", None) if ctx is not None else None
    try:
        # CE-only: a missing frozen migration authority fails the decision;
        # it cannot authorize a Legacy prompt fallback.
        mig = True
        if mig:
            bridge = getattr(ctx, "context_llm_invoker", None) if ctx is not None else None
            if bridge is None or not getattr(bridge, "available", False):
                logger.warning("prep MIG_PREPARATION=true 但 Invoker 不可用")
                return AgentDecision(
                    action="fail",
                    decision_summary="LLM 调用失败: invoker_unavailable",
                    public_update="准备阶段 LLM 调用失败,使用默认方案继续。",
                    expected_result="fallback to legacy KB path",
                    confidence=0.0,
                )
            bres = await bridge.generate(
                user_id=getattr(ctx, "user_internal_id", 0),
                call_site="test_plan.preparation.decide",
                llm_task_profile=PREPARATION_PROFILE,
                current_node="preparation_decide",
                current_goal=user_content,
                task_state_ref=task_state_ref,
                output_contract="preparation_decision_json",
                # The decision prompt is server-owned policy, not task
                # evidence.  Passing it explicitly keeps the CE migration
                # behaviour identical to the former profile invocation.
                system_prompt=system_prompt,
                user_content=user_content,
                conversation_id=getattr(ctx, "conversation_internal_id", None),
                task_id=getattr(ctx, "task_internal_id", None),
                runtime_context=ctx,
            )
            if bres is None:
                return AgentDecision(
                    action="fail",
                    decision_summary="LLM 调用失败: bridge_none",
                    public_update="准备阶段 LLM 调用失败,使用默认方案继续。",
                    expected_result="fallback to legacy KB path",
                    confidence=0.0,
                )
            raw = bres.as_profile_result()
        else:
            return AgentDecision(
                action="fail",
                decision_summary="MIGRATION_CONTEXT_REQUIRED",
                public_update="准备阶段需要 Context Engine，当前调用已被安全阻断。",
                expected_result="context_engine_required",
                confidence=0.0,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("prep llm call failed: %s", exc)
        return AgentDecision(
            action="fail",
            decision_summary=f"LLM 调用失败: {exc}",
            public_update="准备阶段 LLM 调用失败,使用默认方案继续。",
            expected_result="fallback to legacy KB path",
            confidence=0.0,
        )

    parsed = getattr(raw, "parsed", None) if raw is not None else None
    if not getattr(raw, "success", False) or parsed is None:
        # 已经过 profile.on_parse_failure FALLBACK_DEFAULT;若仍 None,显式 fail
        return AgentDecision(
            action="fail",
            decision_summary=str(getattr(raw, "error_message", "") or "LLM parse failed"),
            public_update="准备阶段解析失败,使用默认方案继续。",
            expected_result="fallback to legacy KB path",
            confidence=0.0,
        )

    # parsed 可能是 str (fallback_text) 或 dict (JSON 解析成功)
    raw_val = parsed if isinstance(parsed, str) else _safe_json_dumps(parsed)
    try:
        return AgentDecision.model_validate_json(raw_val)
    except Exception as exc:  # noqa: BLE001
        logger.warning("prep AgentDecision validation failed: %s raw=%r", exc, raw_val[:200])
        return AgentDecision(
            action="fail",
            decision_summary=f"AgentDecision schema 校验失败: {exc}",
            public_update="准备阶段决策结构非法,使用默认方案继续。",
            expected_result="fallback to legacy KB path",
            confidence=0.0,
        )


def _base_prep_state_ref(state: Dict[str, Any]) -> dict:
    """从 state 提取轻量 TaskStateRef（供 bridge 显式传参）。"""
    ref: dict = {}
    for key in ("task_goal", "task_type", "locked_sections", "requirement_summary"):
        val = state.get(key)
        if val not in (None, "", [], {}):
            ref[key] = val
    return ref


def _prep_state_ref(state: Dict[str, Any]) -> dict:
    """Project parsed requirement/template facts into required CE Evidence.

    Preparation occurs after the parser nodes.  It does not need the
    generator's full-document evidence bundle, but its CE Profile does require
    a bounded task-local Evidence section.  Passing only scalar task metadata
    makes selection fail before the Preparation decision model can run.
    """
    ref = _base_prep_state_ref(state)

    parsed_documents: list[dict[str, Any]] = []
    requirement = state.get("requirement_analysis")
    if isinstance(requirement, dict):
        projection = {
            "document_title": requirement.get("document_title"),
            "document_name": requirement.get("document_name"),
            "project_name": requirement.get("project_name"),
            "requirement_summary": state.get("requirement_summary"),
            "document_structure": requirement.get("document_structure") or [],
            "table_summaries": requirement.get("table_summaries") or [],
            "image_count": requirement.get("image_count") or 0,
            "image_texts": requirement.get("image_texts") or [],
        }
        parsed_documents.append(
            {
                "source_ref": str(
                    requirement.get("source_ref")
                    or requirement.get("requirement_file_id")
                    or "task:requirement:parsed"
                ),
                "title": str(
                    requirement.get("document_title")
                    or requirement.get("document_name")
                    or "parsed requirement"
                ),
                "content": json.dumps(projection, ensure_ascii=False, sort_keys=True),
            }
        )

    template_sections: list[dict[str, Any]] = []
    template = state.get("template_structure")
    if isinstance(template, dict):
        projection = {
            "template_name": template.get("template_name"),
            "summary": template.get("summary") or {},
            "sections": template.get("sections") or [],
            "generation_config": template.get("generation_config") or {},
        }
        template_sections.append(
            {
                "source_ref": "template_structure",
                "title": str(template.get("template_name") or "parsed template structure"),
                "content": json.dumps(projection, ensure_ascii=False, sort_keys=True),
            }
        )

    existing = state.get("context_evidence")
    if isinstance(existing, dict):
        evidence = dict(existing)
        evidence.setdefault("parsed_documents", parsed_documents)
        evidence.setdefault("template_sections", template_sections)
    else:
        evidence = {
            "parsed_documents": parsed_documents,
            "template_sections": template_sections,
        }
    if parsed_documents or template_sections or isinstance(existing, dict):
        ref["context_evidence"] = evidence
    return ref


def _safe_json_dumps(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(obj)


def _audit_step(steps_audit: list, decision: AgentDecision, *, tool_name: str) -> None:
    """Append audit row (compact: no raw LLM text, no full args)."""
    sig = ""
    if decision.tool_arguments:
        sig = args_signature(decision.tool_arguments)
    steps_audit.append({
        "decision_summary": (decision.decision_summary or "")[:500],
        "action": decision.action,
        "tool_name": (tool_name or "")[:80],
        "args_signature": sig[:12],
        "outcome": "decided",
        "confidence": decision.confidence,
    })


def _audit_blocked(
    steps_audit: list, decision: AgentDecision, blocked: List[Dict[str, Any]]
) -> None:
    """Append a blocked audit row (Rule 12 + Rule 13 trace)."""
    sig = ""
    if decision.tool_arguments:
        sig = args_signature(decision.tool_arguments)
    steps_audit.append({
        "decision_summary": (decision.decision_summary or "")[:500],
        "action": decision.action,
        "tool_name": (decision.tool_name or "")[:80],
        "args_signature": sig[:12],
        "outcome": "blocked",
        "blocked": blocked[:3],  # 最多 3 条违规
    })


def _gaps_to_questions(decision: AgentDecision) -> list[Any]:
    """Expose the LLM-owned structured gaps to the existing card contract."""
    from app.agent_runtime.preparation.schemas import UserQuestion
    return [
        UserQuestion(field=gap.field, question=gap.description)
        for gap in decision.clarification_gaps
    ]


def _decision_public_update(
    decision: AgentDecision,
    state: Dict[str, Any] | None = None,
) -> Any:
    """Prefer the LLM narrative, preserving a safe text-only fallback.

    The structured narrative is optional so that an otherwise valid LLM action
    cannot fail solely because one display field is missing.  When the model
    supplies only the legacy ``public_update`` text, keep that model-authored
    sentence visible inside a complete public narrative envelope instead of
    replacing it with the generic deterministic fallback.
    """
    if decision.action == "ask_user":
        return _clarification_decision_update(decision, state)
    if decision.decision_update is not None:
        return decision.decision_update
    if decision.observation_update is not None:
        return decision.observation_update

    text = str(decision.public_update or "").strip()
    if not text:
        return None
    next_action = {
        "call_tool": "将检索相关资料后继续评估。",
        "finish": "将进入章节处理策略确认。",
        "fail": "将按默认准备流程继续。",
    }.get(decision.action, "将继续当前准备流程。")
    return {
        "headline": "PreparationAgent 决策",
        "narrative_text": text,
        "summary": text,
        "impact": "该判断将决定后续测试方案准备流程。",
        "next_action": next_action,
        "details": [],
    }


def _clarification_decision_update(
    decision: AgentDecision,
    state: Dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Render one concise Markdown handoff; the card is the sole question owner."""
    category_labels = {
        "scope_and_capacity": "访客类型与角色范围",
        "approval_and_verification": "预约审批与到访核验规则",
        "privacy_and_acceptance": "数据隐私与验收口径",
    }
    categories = [
        category_labels.get(gap.field, gap.field.replace("_", " "))
        for gap in decision.clarification_gaps[:3]
    ]
    observations = [f"已识别 {len(decision.clarification_gaps)} 项会影响测试结论的未决规则。"]
    bundle = (state or {}).get("retrieval_evidence_bundle")
    bundle = bundle if isinstance(bundle, dict) else {}
    company = bundle.get("company_rag") if isinstance(bundle.get("company_rag"), dict) else {}
    project = bundle.get("project_rag") if isinstance(bundle.get("project_rag"), dict) else {}
    if str(company.get("status") or "").lower() == "skipped":
        observations.append("公司规则库未配置，无法提供可采纳的补充依据。")
    if str(project.get("status") or "").lower() == "skipped":
        observations.append("当前会话未关联项目，未发现可检索的项目资料。")
    numbered_observations = [
        f"{index}. {observation}"
        for index, observation in enumerate(observations, start=1)
    ]
    numbered_categories = [
        f"{index}. {category}"
        for index, category in enumerate(categories, start=1)
    ]
    narrative = "\n".join([
        "### 观察",
        *numbered_observations,
        "",
        "### 下一步：需要你确认",
        f"请在下方补充卡中完成 {len(decision.clarification_gaps)} 项关键决策：",
        *numbered_categories,
    ])
    return {
        "headline": "需要补充关键测试规则",
        "narrative_text": narrative[:800],
        "summary": "已识别关键未决规则，等待你的确认。",
        "impact": "补充结果将作为本次测试方案的任务级依据。",
        "next_action": "请填写下方补充卡后继续。",
        "details": [],
    }


def _build_result(
    *,
    information_sufficient: bool,
    knowledge_search_used: bool,
    queries: List[str],
    evidence: List[KnowledgeEvidence],
    requirement_gaps: List[Any],
    user_questions: List[Any],
    constraints: List[str],
    fallback_reason: Optional[str],
    budget_snapshot: BudgetState,
    steps_audit: List[Dict[str, Any]],
) -> PreparationResult:
    """组装最终 PreparationResult。PublicSummary 永远 sanitized。"""
    if fallback_reason:
        headline = "准备阶段已回退到默认方案"
        detail = f"原因:{fallback_reason[:200]}"
        confidence = 0.3
    elif information_sufficient and evidence:
        headline = "信息已收集,准备进入下一步"
        detail = f"已检索 {len(queries)} 次,获得 {len(evidence)} 条证据"
        confidence = 0.85
    elif information_sufficient:
        headline = "现有信息已足够,无需额外检索"
        detail = "需求与模板结构清晰,直接进入下一步"
        confidence = 0.75
    elif user_questions:
        headline = "需要补充信息"
        detail = "发现需求缺口,已记录待追问"
        confidence = 0.4
    else:
        headline = "准备阶段完成"
        detail = "已尽力评估,可进入主流程"
        confidence = 0.5

    # Append steps audit metadata (truncated for Safety)
    if steps_audit:
        last = steps_audit[-1]
        detail = (detail or "") + f" | last_step={last.get('outcome','unknown')}"

    public_summary = PublicSummary(
        headline=headline[:120],
        detail=detail[:480] if detail else None,
    )
    return PreparationResult(
        information_sufficient=information_sufficient,
        knowledge_search_used=knowledge_search_used,
        queries=queries,
        evidence=evidence,
        requirement_gaps=requirement_gaps,
        user_questions=user_questions,
        constraints=constraints,
        confidence=confidence,
        public_summary=public_summary,
        fallback_reason=fallback_reason,
        budget_state=budget_snapshot,
    )


__all__ = ["run_preparation", "ModeBNotImplemented"]


# module-level note (auto-appended):
# Preparation 主循环入口(run_preparation)。
# 内部步骤: build_understanding / batch_ask_user / build_knowledge_evidence / build_final_requirement。
# BudgetTracker + ToolPermissionGuard 贯穿始终;失败转 run_legacy_kb_fallback。
# 关键约束: 不允许直接调 LLM, 不允许写 DB。
