"""v2 条件边纯函数。

每个 router 都是 ``Callable[[TestPlanGraphState], str]``,不写 DB,不发事件。
仅用来决定图结构中的下一节点名。
"""

from __future__ import annotations

from app.agent_runtime.graphs.test_plan.constants import MAX_FORMAT_LOOPS, MAX_REVIEW_LOOPS
from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState


NODE_PARSE_REQUIREMENT = "parse_requirement"
NODE_FAIL_TASK = "fail_task"
NODE_REGENERATE_SECTIONS_STEP = "regenerate_sections"
NODE_PREPARE_EXPORT = "prepare_export"
NODE_FINALIZE_TASK = "finalize_task"
NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION = "pause_for_legacy_format_decision"

# Phase 2.3: Preparation Agent dynamic subgraph routing
NODE_SEARCH_KNOWLEDGE = "search_knowledge"
NODE_SUGGEST_SECTIONS = "suggest_sections"
NODE_PREP_SUBGRAPH = "prep_subgraph"
NODE_PREP_LEGACY_FALLBACK = "prep_legacy_fallback"

# Phase 2.4: Review Repair Agent dynamic subgraph routing
NODE_REPAIR_SUBGRAPH = "repair_subgraph"


# ── validate_inputs ──────────────────────────────────────────────────────────


def route_after_validate(state: TestPlanGraphState) -> str:
    """``validate_inputs`` → ``parse_requirement`` or ``fail_task``.

    失败路径:``last_error`` 非空 → ``fail_task``;否则正常进入。
    """
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
    if state.get("preparation_agent_enabled"):
        return NODE_PREP_SUBGRAPH
    return NODE_SEARCH_KNOWLEDGE


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

    # Phase 2.4 — read new review_issues first, fall back to legacy block_issues
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
    * failed AND 已达上限 — 合成 losses → pause_for_legacy_format_decision
    * loss_detected AND format_loop_count ≤ MAX_FORMAT_LOOPS → pause_for_legacy_format_decision
    """
    result = state.get("format_check_result") or {}
    level = str(result.get("level") or "")
    loops = int(state.get("format_loop_count") or 0)

    if level in ("passed", "warning"):
        return NODE_FINALIZE_TASK

    if level == "loss_detected" and loops <= MAX_FORMAT_LOOPS:
        return NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION

    if level == "failed":
        if loops < MAX_FORMAT_LOOPS:
            return NODE_PREPARE_EXPORT
        return NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION

    # 兜底:未识别 level 一律 pause 确认(防御性)
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
    "NODE_REPAIR_SUBGRAPH",
    "route_after_validate",
    "route_after_review",
    "route_after_format_check",
    "route_after_parse_template",
]


# ════════════════════════════════════════════════════════════════════════════════
# v2_frozen 历史快照(Phase 2.2)。
# 与同目录的 v2/ 字节级镜像;目录独立是为了 Phase 2.2.x 升级不被绑死,
# v2 可演进,v2_frozen 永远冻结(供回滚 / 测试稳定快照用)。
#
# 读代码时:v2_frozen 内容与 v2 几乎一致,优先看 v2 的注释(v2 改动先行)。
# 唯一差异:LangGraph 编译时 graph_name 不同
# (`${GRAPH_NAME_TEST_PLAN}_v2_frozen` vs `${GRAPH_NAME_TEST_PLAN}_v2`),
# 不影响代码组织。
#
# ⚠️ 不要让 v2_frozen 与 v3 直接对照读 ——
#   v2_frozen 用 sentinel pause_marker,v3 走真 LangGraph interrupt,差异大;
#   想知道中断怎么跑,读 v3/nodes_interrupts.py(v3 已加注释)。
# ════════════════════════════════════════════════════════════════════════════════
