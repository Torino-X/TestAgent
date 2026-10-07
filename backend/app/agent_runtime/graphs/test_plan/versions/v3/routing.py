"""v2 条件边纯函数。

每个 router 都是 ``Callable[[TestPlanGraphState], str]``,不写 DB,不发事件。
仅用来决定图结构中的下一节点名。
"""

from __future__ import annotations

from app.agent_runtime.graphs.test_plan.constants import MAX_FORMAT_LOOPS, MAX_REVIEW_LOOPS
from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState


NODE_PARSE_REQUIREMENT = "parse_requirement"
NODE_PARSE_TEMPLATE = "parse_template"
NODE_FAIL_TASK = "fail_task"
NODE_REGENERATE_SECTIONS_STEP = "regenerate_sections"
NODE_PREPARE_EXPORT = "prepare_export"
NODE_FINALIZE_TASK = "finalize_task"
NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION = "pause_for_legacy_format_decision"
# Phase 2.9A.7: 真 interrupt 路径新增节点 — prepare 节点决定走
# section_confirmation_interrupt(继续)还是 fail_task(失败)。
NODE_SECTION_CONFIRM_INTERRUPT = "section_confirmation_interrupt"
NODE_PREPARE_SECTION_CONFIRMATION = "prepare_section_confirmation"

# Phase 2.3: Preparation Agent dynamic subgraph routing
NODE_SEARCH_KNOWLEDGE = "search_knowledge"
NODE_SUGGEST_SECTIONS = "suggest_sections"
NODE_PREP_SUBGRAPH = "prep_subgraph"
NODE_PREP_LEGACY_FALLBACK = "prep_legacy_fallback"
NODE_PREPARE_CLARIFICATION = "prepare_preparation_clarification"

# A task may refine its evidence plan once after the initial retrieval.  The
# bound is deterministic so an LLM cannot create an unbounded query loop.
MAX_PREPARATION_RETRIEVAL_ROUNDS = 2


def _canonical_query(value: object) -> str:
    return " ".join(str(value or "").lower().split())[:720]


def _has_clarification_request(result: dict) -> bool:
    return bool(result.get("requirement_gaps") or result.get("user_questions"))


def _has_accepted_clarification(state: TestPlanGraphState) -> bool:
    """Return whether the current task already has an authoritative answer.

    A user can either fill an answer or explicitly choose a permitted
    conservative scope. Both resolve the current clarification round and must
    take precedence over a follow-up retrieval proposal from the model.
    """
    response = state.get("clarification_answers")
    if not isinstance(response, dict):
        return False
    answers = response.get("answers")
    if isinstance(answers, dict) and any(str(value).strip() for value in answers.values()):
        return True
    conservative_ids = response.get("conservative_gap_ids")
    return isinstance(conservative_ids, list) and bool(conservative_ids)


def _sources_exhausted(state: TestPlanGraphState) -> bool:
    bundle = state.get("retrieval_evidence_bundle")
    if not isinstance(bundle, dict):
        return False
    terminal_statuses = {"skipped", "degraded", "not_available", "unavailable"}
    source_statuses = []
    for source in ("company_rag", "project_rag"):
        entry = bundle.get(source)
        if not isinstance(entry, dict):
            return False
        source_statuses.append(str(entry.get("status") or "").lower())
    return bool(source_statuses) and all(status in terminal_statuses for status in source_statuses)


def _requested_query_signature(state: TestPlanGraphState, result: dict) -> str:
    queries = result.get("queries") or []
    if isinstance(queries, list):
        for query in queries:
            signature = _canonical_query(query)
            if signature:
                return signature
    plan = state.get("retrieval_plan_snapshot")
    if isinstance(plan, dict):
        return _canonical_query(plan.get("company_query") or plan.get("query"))
    return ""

# Phase 2.4: Review Repair Agent dynamic subgraph routing
NODE_REPAIR_SUBGRAPH = "repair_subgraph"
NODE_REPAIR_FALLBACK = "repair_fallback_step"

# Only failures that the deterministic regeneration path can address may leave
# RepairAgent. Authentication, quota and model-availability failures stay
# terminal instead of being retried as malformed model output.
_RECOVERABLE_REPAIR_FALLBACK_PREFIXES = (
    "context_selection_required_unmet:",
    "llm_parse_error",
    "decision_validation_error",
    "json_validation_failed",
)


def is_recoverable_repair_fallback(reason: object) -> bool:
    """Return whether a RepairAgent failure may safely enter regeneration."""
    value = str(reason or "").strip().lower()
    return bool(value) and value.startswith(_RECOVERABLE_REPAIR_FALLBACK_PREFIXES)

# Phase 2.9A.9: generate_test_plan 后的 Fail-Fast 路由 — 失败走 fail_task,
# 不继续 ResultReviewTool / WordExportTool / DocxFormatCheckTool。
NODE_GENERATE_TEST_PLAN = "generate_test_plan"
NODE_REVIEW_STEP = "review_step"

# Phase 2.9B.4: tool_narrative_barrier 后的路由 — 读取 state.next_node。
NODE_TOOL_NARRATIVE_BARRIER = "tool_narrative_barrier"


# ── tool_narrative_barrier 路由 (Phase 2.9B.4) ─────────────────────────────


def route_after_narrative(state: TestPlanGraphState) -> str:
    """``tool_narrative_barrier`` → continuation_route。

    叙事完成后,由 barrier 节点把 continuation_route 写入 ``state.next_node``;
    本函数读取并返回。缺省回落 ``parse_template``。
    """
    # 设计: barrier 是"工具后必经的同步叙事屏障",不能依赖 barrier 自己去选下游;
    # 由上游 Tool 在 pending_narrative.continuation_route 显式告知下一节点,
    # 这样 barrier 自己保持纯函数(只决定"是否放行 + 落到 next_node"),便于测试。
    return state.get("next_node") or NODE_PARSE_TEMPLATE


# ── validate_inputs ──────────────────────────────────────────────────────────


def route_after_validate(state: TestPlanGraphState) -> str:
    """``validate_inputs`` → ``parse_requirement`` or ``fail_task``.

    失败路径:``last_error`` 非空 → ``fail_task``;否则正常进入。
    """
    # 校验节点失败时把结构化错误写入 state.last_error(例如文件缺失 / 必填字段空);
    # 这里看到 last_error 就直接路由到 fail_task,不等后续节点报更深的异常。
    if state.get("last_error"):
        return NODE_FAIL_TASK
    return NODE_PARSE_REQUIREMENT


# ── parse_template (Phase 2.3 增量:prep 路由) ───────────────────────────


def route_after_parse_template(state: TestPlanGraphState) -> str:
    """``parse_template`` → ``prep_subgraph`` 或 ``search_knowledge``.

    Phase 2.3 增量 — 仅当 ``preparation_agent_enabled=True`` 时走 prep subgraph;
    否则保持 Phase 2.1 行为直接进 ``search_knowledge`` (字节级兼容)。

    纯函数: 仅读 ``state["preparation_agent_enabled"]``,不写 DB / 不发事件。
    """
    # preparation_agent_enabled 由 feature flag 在 make_empty_state 时写入;
    # True 时进入 prep_subgraph 跑 PreparationAgent 子图(ask_user / 多步执行),
    # 关闭时保持旧行为直接进知识库检索,保证老任务字节级兼容。
    if state.get("preparation_agent_enabled"):
        return NODE_PREP_SUBGRAPH
    return NODE_SEARCH_KNOWLEDGE


def route_after_preparation(state: TestPlanGraphState) -> str:
    """Choose the next evidence step after a PreparationAgent evaluation.

    Preparation may finish immediately when the uploaded requirement already
    contains all required facts. A structured ``ask_user`` result is terminal:
    the clarification card owns the full questions and must not be delayed by
    a speculative second lookup. The remaining retrieval path is bounded to
    genuinely new queries with an available source.
    """
    result = state.get("preparation_result")
    if not isinstance(result, dict):
        return NODE_PREP_LEGACY_FALLBACK
    if result.get("fallback_reason"):
        return NODE_PREP_LEGACY_FALLBACK
    # The submitted card response is the task-level source of truth. A model
    # may still suggest another retrieval after it, but turning that proposal
    # into another clarification card traps the task in a confirmation loop.
    if _has_accepted_clarification(state):
        return NODE_SUGGEST_SECTIONS
    if bool(result.get("information_sufficient")):
        return NODE_SUGGEST_SECTIONS
    if _has_clarification_request(result):
        return NODE_PREPARE_CLARIFICATION
    if _sources_exhausted(state):
        return NODE_PREPARE_CLARIFICATION
    signature = _requested_query_signature(state, result)
    known_signatures = {
        _canonical_query(item)
        for item in (state.get("retrieval_query_signatures") or [])
        if _canonical_query(item)
    }
    if signature and signature in known_signatures:
        return NODE_PREPARE_CLARIFICATION
    if int(state.get("retrieval_round") or 0) < MAX_PREPARATION_RETRIEVAL_ROUNDS:
        return NODE_SEARCH_KNOWLEDGE
    # Two evidence rounds are exhausted and the requirement is still not
    # sufficient. Never fall through to section generation.
    return NODE_PREPARE_CLARIFICATION


# ── prepare_section_confirmation(Phase 2.9A.7)──────────────────────────


def route_after_prepare_section_confirmation(state: TestPlanGraphState) -> str:
    """``prepare_section_confirmation`` → interrupt 等待 or fail。

    决策:
    * ``task_status=failed`` (持久化失败 / 缺 sections) → ``fail_task``
      节点(让 graph 进入 FAIL_TASK 终态,Worker 可看到明确 error_code)。
    * 其它 → ``section_confirmation_interrupt`` 节点(同步 interrupt,
      等用户决策)。

    纯函数:仅读 ``state["task_status"]``,不写 DB / 不发事件。
    """
    # prepare 节点负责落库 HumanConfirmation 行 + emit 章节确认事件;它失败时
    # 通常是因为持久化错误或 section 列表为空 — 这种状态没法 interrupt 让用户救,
    # 必须 fail_task 让 Worker 看到 error_code 才能重试或告警。
    if state.get("task_status") == "failed":
        return NODE_FAIL_TASK
    return NODE_SECTION_CONFIRM_INTERRUPT


# ── generate_test_plan (Phase 2.9A.9) ────────────────────────────────────


def route_after_generate_test_plan(state: TestPlanGraphState) -> str:
    """``generate_test_plan`` → ``review_step`` / ``fail_task``。

    Phase 2.9A.9 Fail-Fast:
    * ``task_status=failed``(状态验证失败 / TestPlanGeneratorTool 失败)
      → ``fail_task``,不继续 review/export/format_check。
    * 其它 → ``review_step``(原拓扑)。

    纯函数:仅读 ``state["task_status"]`` + ``state["last_error"]``,
    不写 DB / 不发事件。
    """
    # Fail-Fast:生成节点失败意味着没有合法的 section_package,下游 review /
    # export / format_check 都会因为数据缺失而失败,直接 fail_task 让 Worker
    # 看到明确 error_code 决定重试或告警。
    if state.get("task_status") == "failed":
        return NODE_FAIL_TASK
    return NODE_REVIEW_STEP


# ── result_review (Phase 2.9A.10) ────────────────────────────────────────────


def route_after_result_review(state: TestPlanGraphState) -> str:
    """``review_step`` → ``regenerate_sections`` / ``prepare_export`` / ``fail_task``。

    Phase 2.9A.10 Fail-Fast 优先级:
    * ``task_status=failed``(REVIEW_CONTENT_MISSING / 工具执行失败)
      → ``fail_task``,不继续 export/format_check。
    * ``level=failed`` + 有 block issues + repair_agent_enabled + 计数允许
      → ``repair_subgraph``(原 Phase 2.4 行为)。
    * ``level=failed`` + 有 block issues + review_loop_count < MAX
      → ``regenerate_sections``(原 Phase 2.1 regen 行为)。
    * 其它(passed/warning/已超上限)→ ``prepare_export``。

    Phase 2.9A.10 关键:工具执行失败(review_input_present=False 或
    envelope.success=False)与 review level=warning/failed 必须分开 —
    工具执行失败 = 立刻 fail_task;review level 不通过 = 修复路径。
    """
    if state.get("task_status") == "failed":
        return NODE_FAIL_TASK

    review = state.get("review_result") or {}
    level = str(review.get("level") or "")

    review_issues = review.get("review_issues") or []
    if review_issues:
        block_issues = [
            i for i in review_issues
            if isinstance(i, dict) and i.get("severity") == "block"
        ]
    else:
        block_issues = review.get("block_issues") or []
    loops = int(state.get("review_loop_count") or 0)
    repair_loops = int(state.get("repair_loop_count") or 0)

    # Phase 2.4 — Repair Agent 接管
    if (
        level == "failed"
        and block_issues
        and state.get("repair_agent_enabled")
        and repair_loops < MAX_REVIEW_LOOPS
    ):
        return NODE_REPAIR_SUBGRAPH

    # Phase 2.1 — legacy regen 路径(向后兼容)
    if (
        level == "failed"
        and block_issues
        and loops < MAX_REVIEW_LOOPS
    ):
        return NODE_REGENERATE_SECTIONS_STEP

    # A failed review with blocking issues must never be exported merely because
    # the bounded recovery budget was consumed.
    if level == "failed" and block_issues:
        return NODE_FAIL_TASK

    # pass-through(passed / warning) → prepare_export
    return NODE_PREPARE_EXPORT


def route_after_repair(state: TestPlanGraphState) -> str:
    """``repair_subgraph`` → ``prepare_export`` / ``fail_task``.

    RepairAgent 只有在复审通过时才算 clean finish。任何未解决的阻断问题
    （包括预算耗尽）都不得进入 Word 导出，避免把已知不符合模板契约的内容
    伪装成可交付产物。
    """
    repair = state.get("repair_result") or {}
    if isinstance(repair, dict) and repair.get("review_passed"):
        return NODE_PREPARE_EXPORT

    fallback_reason = state.get("repair_fallback_reason")
    if not fallback_reason and isinstance(repair, dict):
        fallback_reason = repair.get("fallback_reason")
    if is_recoverable_repair_fallback(fallback_reason):
        return NODE_REPAIR_FALLBACK

    if state.get("task_status") == "failed":
        return NODE_FAIL_TASK

    return NODE_FAIL_TASK


# ── export_word (Phase 2.9A.9) ───────────────────────────────────────────


def route_after_export_word(state: TestPlanGraphState) -> str:
    """``export_word`` → ``check_docx_format`` / ``fail_task``。

    Phase 2.9A.9 Fail-Fast:WordExportTool 失败 → 不进 format_check。
    """
    if state.get("task_status") == "failed":
        return NODE_FAIL_TASK
    return "check_docx_format_step"


# ── check_docx_format (Phase 2.9A.9) ─────────────────────────────────────


def route_after_check_format_strict(state: TestPlanGraphState) -> str:
    """``check_docx_format`` 严格版:passed/warning 才进 finalize,其它走原拓扑。

    Phase 2.9A.9:Fail-Fast — 失败/loss_detected 不会被 silent export 到
    finalize_task;走原 pause/export/retry 路径。
    """
    return route_after_format_check(state)


# ── review_result ────────────────────────────────────────────────────────────


def route_after_review(state: TestPlanGraphState) -> str:
    """``review_result`` → ``regenerate_sections`` / ``repair_subgraph`` / ``prepare_export``.

    决策表(Phase 2.4 升级, Phase 2.1 行为向后兼容):
    * level=failed 且无 block issues → ``prepare_export``
    * level=failed AND block issues AND repair_agent_enabled AND repair_loop_count < MAX_REVIEW_LOOPS
      → ``repair_subgraph`` (Phase 2.4 Repair Agent 接管)
    * level=failed AND block issues AND review_loop_count < MAX_REVIEW_LOOPS
      → ``regenerate_sections`` (Phase 2.1 固定 regen 路径)
    * 其它(通过 / 已超上限)→ ``prepare_export``

    优先级: pass-through > repair_agent > legacy regen > export
    """
    review = state.get("review_result") or {}
    level = str(review.get("level") or "")

    # Phase 2.4 — read new review_issues first, fall back to legacy block_issues.
    # An explicit empty review_issues list is authoritative: it means the latest
    # ResultReviewTool pass found no block issues, even if legacy fields remain.
    review_issues = review.get("review_issues")
    if isinstance(review_issues, list):
        block_issues = [
            i for i in review_issues
            if isinstance(i, dict) and i.get("severity") == "block"
        ]
    else:
        block_issues = review.get("block_issues") or []
    loops = int(state.get("review_loop_count") or 0)
    repair_loops = int(state.get("repair_loop_count") or 0)

    # Phase 2.8C:fire-and-forget repair 调度(旁路,不影响 graph routing)。
    # 仅当 review failed + 有 block issues 时调;ApiDispatcher /
    # dynamic_agent_api_enabled 不可用则 no-op(纯函数兜底)。
    if level == "failed" and block_issues:
        _try_fire_repair_dispatch(state)

    # pass-through
    if level != "failed" or not block_issues:
        return NODE_PREPARE_EXPORT

    # Phase 2.4: Repair Agent 接管
    if (
        state.get("repair_agent_enabled")
        and repair_loops < MAX_REVIEW_LOOPS
    ):
        return NODE_REPAIR_SUBGRAPH

    # Phase 2.1: legacy regen 路径(向后兼容)
    if loops >= MAX_REVIEW_LOOPS:
        return NODE_FAIL_TASK

    if loops < MAX_REVIEW_LOOPS:
        return NODE_REGENERATE_SECTIONS_STEP

    # 兜底:已超上限 → 强制 export
    return NODE_PREPARE_EXPORT


def _try_fire_repair_dispatch(state: TestPlanGraphState) -> None:
    """Phase 2.8C ADR-2.8C-9:review failed 时 fire-and-forget 调 dispatch_repair_task。

    仅当 ApiDispatcher 在 ``state["runtime_context"].app_state.api_dispatcher``
    可用 + ``dynamic_agent_api_enabled=True`` 时触发;否则 no-op(纯函数兜底)。

    异常仅 WARN,不阻塞 graph 路由(否则 review_failed 流程全部崩溃)。
    """
    try:
        ctx = state.get("runtime_context")
        if ctx is None:
            return
        app_state = getattr(ctx, "app_state", None)
        if app_state is None:
            return
        api_dispatcher = getattr(app_state, "api_dispatcher", None)
        if api_dispatcher is None:
            return
        flags = getattr(api_dispatcher, "feature_flags", None)
        if flags is None or not getattr(flags, "dynamic_agent_api_enabled", False):
            return
        task_public_id = str(getattr(ctx, "task_public_id", "") or "")
        if not task_public_id:
            return
        # Fire-and-forget:在事件循环里 schedule,不 await
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if loop.is_running():
            asyncio.ensure_future(
                api_dispatcher.dispatch_repair_task(
                    task_public_id=task_public_id,
                    task_engine_type="repair",
                    payload={
                        "task_id": task_public_id,
                        "review_issues": state.get("review_result", {}).get(
                            "review_issues"
                        ),
                        "block_issues": state.get("review_result", {}).get(
                            "block_issues"
                        ),
                        "source": "route_after_review",
                    },
                ),
                loop=loop,
            )
    except Exception as exc:  # noqa: BLE001 — fire-and-forget must not break routing
        import logging
        logging.getLogger(__name__).warning(
            "route_after_review: fire_repair_dispatch failed; ignore | err=%s",
            exc,
        )


# ── check_docx_format ────────────────────────────────────────────────────────


def route_after_format_check(state: TestPlanGraphState) -> str:
    """``check_docx_format`` → ``finalize_task`` / ``prepare_export`` / ``pause_for_legacy_format_decision``.

    决策表:
    * passed / warning → finalize_task
    * failed AND format_loop_count < MAX_FORMAT_LOOPS → prepare_export(下一保真度重试)
    * blocked(原 loss_detected) AND format_loop_count ≤ MAX_FORMAT_LOOPS →
      pause_for_legacy_format_decision
    * failed AND 已达上限 — 合成 losses → pause_for_legacy_format_decision
    * 兜底:未识别 level 一律 pause 确认(防御性)

    Phase 2.9A.18:字面值统一为 ``passed / warning / blocked / failed``。
    历史 Envelop / check tool 仍可能在 level 字段返回 ``loss_detected`` —
    这里同时识别,做到向后兼容。
    """
    result = state.get("format_check_result") or {}
    # Phase 2.9A.18:status 字段是首选;level 仅做向后兼容 ——
    # 历史 task 可能把 status / level 放在两个不同的字段,都读一下保证向后兼容。
    level = (
        str(result.get("status") or result.get("level") or "")
        .strip()
        .lower()
    )
    # 旧 task level 字段取值 "loss_detected"(字符串),与新字面值对齐为 "blocked"
    if level == "loss_detected":
        level = "blocked"
    loops = int(state.get("format_loop_count") or 0)

    # 通过 / 警告 → 直接 finalize,产物是可下发的
    if level in ("passed", "warning"):
        return NODE_FINALIZE_TASK

    # 阻塞(原 loss_detected):需要用户决策 → 暂停走 legacy format_loss pause,
    # 前端弹出 format-loss 卡片让用户选 accept/retry/reject。
    if level == "blocked" and loops <= MAX_FORMAT_LOOPS:
        return NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION

    # 失败:未达上限 → 重新准备导出(降低保真度),走 prepare_export 让
    # WordExportTool 重新跑一遍;已达上限 → 暂停等用户决策。
    if level == "failed":
        if loops < MAX_FORMAT_LOOPS:
            return NODE_PREPARE_EXPORT
        return NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION

    # 兜底:任何未识别的 level 都走"让用户处理",而不是直接 export/finalize,
    # 避免未知状态被静默放行到 finalize_task。
    return NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION


__all__ = [
    "NODE_PARSE_REQUIREMENT",
    "NODE_FAIL_TASK",
    "NODE_REGENERATE_SECTIONS_STEP",
    "NODE_PREPARE_EXPORT",
    "NODE_FINALIZE_TASK",
    "NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION",
    "NODE_SEARCH_KNOWLEDGE",
    "NODE_SUGGEST_SECTIONS",
    "NODE_PREP_SUBGRAPH",
    "NODE_PREP_LEGACY_FALLBACK",
    "NODE_PREPARE_CLARIFICATION",
    "MAX_PREPARATION_RETRIEVAL_ROUNDS",
    "NODE_REPAIR_SUBGRAPH",
    "NODE_REPAIR_FALLBACK",
    "NODE_SECTION_CONFIRM_INTERRUPT",
    "NODE_PREPARE_SECTION_CONFIRMATION",
    # Phase 2.9A.9
    "NODE_GENERATE_TEST_PLAN",
    "NODE_REVIEW_STEP",
    "route_after_validate",
    "route_after_review",
    "route_after_format_check",
    "route_after_parse_template",
    "route_after_preparation",
    "route_after_prepare_section_confirmation",
    "route_after_generate_test_plan",
    "route_after_export_word",
    "route_after_result_review",  # Phase 2.9A.10
    "route_after_repair",
    "is_recoverable_repair_fallback",
]
