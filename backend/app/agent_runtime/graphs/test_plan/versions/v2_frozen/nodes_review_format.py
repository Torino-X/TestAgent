"""v2 review-regen + format-check 节点 (Legacy F025 + F025-ext 段)。

节点:
* ``review_result``       — ResultReviewTool;决定走 prepare_export 还是 regenerate_sections
* ``regenerate_sections`` — TestPlanRegenTool;失败块修复
* ``check_docx_format``   — DocxFormatCheckTool;FAILED/PASSED/loss_detected
* ``repair_subgraph``     — Phase 2.4 Repair Agent 入口(由 route_after_review 触发)
* ``repair_fallback``     — Phase 2.4 字节级复用 regenerate_sections_node

条件边在 ``routing.py`` 给出,本文件只写节点。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from app.agent.enums import AgentEventType, TaskStatus

from app.agent_runtime.runtime_context import RuntimeContext

from app.agent_runtime.graphs.test_plan.constants import MAX_REVIEW_LOOPS
from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState
from .nodes_util import mark_completed

logger = logging.getLogger(__name__)


NODE_REVIEW_STEP = "review_step"
NODE_REGENERATE_SECTIONS_STEP = "regenerate_sections_step"
NODE_CHECK_FORMAT_STEP = "check_docx_format_step"

# Phase 2.4 — Repair subgraph entry/exit node names
NODE_REPAIR_SUBGRAPH = "repair_subgraph_step"
NODE_REPAIR_FALLBACK = "repair_fallback_step"


def _adapter(ctx: RuntimeContext):
    return getattr(ctx, "tool_adapter", None)


def _subset_generation_config(
    gen_config: Any,
    section_ids: list,
) -> Dict[str, Any]:
    """Phase 2.4 — ADR-2.4-3 bug-fix #2: 派生 generation_config_subset.

    Mirror of legacy ``app/agent/orchestrator.py`` ``_subset_generation_config`` —
    若 ``gen_config.sections`` 存在,只保留目标 sections;否则整段透传。
    """
    if not isinstance(gen_config, dict):
        return {}
    full_sections = gen_config.get("sections")
    if isinstance(full_sections, list):
        keep = set(section_ids or [])
        subset_sections = [
            s for s in full_sections
            if isinstance(s, dict) and (
                not keep
                or s.get("section_id") in keep
                or s.get("id") in keep
                or s.get("title") in keep
            )
        ]
        return {**gen_config, "sections": subset_sections}
    return dict(gen_config)


# ── review_result ────────────────────────────────────────────────────────────


async def review_result_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """调 ResultReviewTool;产出 review_result + level(+/ warning / failed)。

    review_loop_count 自增;route_after_review 看是否继续 regenerate。

    Phase 2.4 — ADR-2.4-3 bug-fix #1 + ADR-2.4-12: review_result 同时写
    ``review_issues`` (新,标准化格式) + ``block_issues`` (旧, 兼容 Phase 2.1 测试)。
    """
    completed = mark_completed(state, NODE_REVIEW_STEP)
    adapter = _adapter(ctx)
    loops = int(state.get("review_loop_count") or 0)
    new_loops = loops + 1

    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_REVIEW_STEP,
        event_type=AgentEventType.REVIEW_COMPLETED.value,
        title="结果审查",
        content="",
        payload={"review_loop_count": new_loops, "max_loops": MAX_REVIEW_LOOPS},
    )

    if adapter is None:
        return {
            "review_result": {"level": "passed", "issues": [], "block_issues": []},
            "review_loop_count": new_loops,
            "current_node": NODE_REVIEW_STEP,
            "completed_nodes": completed,
        }

    envelope = await adapter.execute(
        tool_name="ResultReviewTool",
        inputs={
            "test_plan_content": state.get("test_plan_content") or {},
            "review_standard": state.get("review_standard") or {},
            "previous_review": state.get("review_result") or {},
        },
        ctx_runtime=ctx,
    )

    data = envelope.get("data") or {}
    return {
        "review_result": data if isinstance(data, dict) else {"level": "failed", "issues": []},
        "review_loop_count": new_loops,
        "current_node": NODE_REVIEW_STEP,
        "completed_nodes": completed,
    }


# ── regenerate_sections ──────────────────────────────────────────────────────


async def regenerate_sections_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """针对 review_issues 中 severity=block 调 TestPlanRegenTool, 就地改 test_plan_content。

    Phase 2.4 — ADR-2.4-3 bug-fix #2: 接受 ``generation_config_subset`` 入参(从
    ``template_structure.generation_config`` 按目标 sections 派生); 同时读
    ``review_issues`` 优先, ``block_issues`` 兜底(向后兼容 Phase 2.1)。
    """
    completed = mark_completed(state, NODE_REGENERATE_SECTIONS_STEP)
    adapter = _adapter(ctx)
    review = state.get("review_result") or {}

    # Phase 2.4: read review_issues first (new), fall back to block_issues (legacy)
    review_issues = review.get("review_issues")
    if isinstance(review_issues, list) and review_issues:
        block_issues = [
            {
                "rule_id": i.get("rule_id"),
                "section_id": i.get("section_id"),
                "message": i.get("message"),
                "issue_id": i.get("issue_id"),
                "field_path": i.get("field_path"),
                "expected_rule": i.get("expected_rule"),
                "repairable": i.get("repairable"),
                "suggested_strategy": i.get("suggested_strategy"),
                "kind": i.get("kind"),
                "severity": i.get("severity"),
                "evidence": i.get("evidence"),
                "span": i.get("span"),
            }
            for i in review_issues
            if i.get("severity") == "block"
        ]
    else:
        block_issues = review.get("block_issues") or review.get("issues") or []

    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_REGENERATE_SECTIONS_STEP,
        event_type=AgentEventType.SECTION_REGENERATING.value,
        title="章节重写中",
        content="",
        payload={"issue_count": len(block_issues) if isinstance(block_issues, list) else 0},
    )

    section_ids: list = []
    issues: list = []
    if isinstance(block_issues, list):
        for issue in block_issues:
            if isinstance(issue, dict):
                if issue.get("section_id"):
                    section_ids.append(issue["section_id"])
                issues.append(issue)

    if adapter is None:
        # 测试:不调 tool
        new_content = dict(state.get("test_plan_content") or {})
        new_content["regenerated"] = True
        return {
            "test_plan_content": new_content,
            "current_node": NODE_REGENERATE_SECTIONS_STEP,
            "completed_nodes": completed,
        }

    # Phase 2.4: derive generation_config_subset from template_structure
    template_structure = state.get("template_structure") or {}
    gen_config = template_structure.get("generation_config") or {}
    generation_config_subset = _subset_generation_config(gen_config, section_ids)

    envelope = await adapter.execute(
        tool_name="TestPlanRegenTool",
        inputs={
            "section_ids": section_ids,
            "issues": issues,
            "test_plan_content": state.get("test_plan_content") or {},
            "template_structure": template_structure,
            "generation_config_subset": generation_config_subset,
        },
        ctx_runtime=ctx,
    )

    data = envelope.get("data") or {}
    merged = dict(state.get("test_plan_content") or {})
    if isinstance(data, dict):
        merged.update(data)
    return {
        "test_plan_content": merged,
        "current_node": NODE_REGENERATE_SECTIONS_STEP,
        "completed_nodes": completed,
    }


# ── check_docx_format ────────────────────────────────────────────────────────


async def check_docx_format_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """调 DocxFormatCheckTool;产出 level(passed/warning/failed/loss_detected)与 losses。"""
    completed = mark_completed(state, NODE_CHECK_FORMAT_STEP)
    adapter = _adapter(ctx)
    loops = int(state.get("format_loop_count") or 0)

    if adapter is None:
        return {
            "format_check_result": {"level": "passed", "losses": []},
            "format_loop_count": 0,
            "current_node": NODE_CHECK_FORMAT_STEP,
            "completed_nodes": completed,
        }

    envelope = await adapter.execute(
        tool_name="DocxFormatCheckTool",
        inputs={
            "artifact": state.get("artifact") or {},
            "format_loss_confirmation": state.get("format_loss_confirmation") or {},
        },
        ctx_runtime=ctx,
    )

    data = envelope.get("data") or {}
    losses = data.get("losses") or []

    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_CHECK_FORMAT_STEP,
        event_type=AgentEventType.DOCX_FORMAT_CHECKED.value,
        title="格式自检",
        content="",
        payload={
            "level": data.get("level") if isinstance(data, dict) else "passed",
            "losses": losses if isinstance(losses, list) else [],
            "loop_count": loops,
        },
    )

    return {
        "format_check_result": data if isinstance(data, dict) else {"level": "passed"},
        "format_loop_count": loops + 1,
        "pending_format_losses": losses if isinstance(losses, list) else [],
        "current_node": NODE_CHECK_FORMAT_STEP,
        "completed_nodes": completed,
    }


# ── Phase 2.4: repair_subgraph_node + repair_fallback_node ──────────────────


async def repair_subgraph_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """Phase 2.4 — Repair Agent dynamic subgraph 入口。

    调 ``repair.subgraph.run_repair`` 完成一轮 Repair;写回 ``repair_result``,
    ``repair_steps``, ``repair_budget_state``, ``repair_loop_count``;
    更新 ``review_result.level`` 以反映修复结果。
    """
    from app.agent_runtime.repair.subgraph import run_repair_subgraph

    completed = mark_completed(state, NODE_REPAIR_SUBGRAPH)
    repair_loops = int(state.get("repair_loop_count") or 0)
    new_repair_loops = repair_loops + 1

    review = state.get("review_result") or {}
    test_plan_content = state.get("test_plan_content") or {}
    template_structure = state.get("template_structure") or {}
    locked_section_ids = list(state.get("locked_section_ids") or [])
    adapter = _adapter(ctx)
    llm_client = getattr(ctx, "llm_client", None)

    if adapter is None or llm_client is None:
        # Fallback: 字节级复用 regenerate_sections_node 主体
        result_state = await regenerate_sections_node(state, ctx=ctx)
        result_state["repair_fallback_reason"] = "missing_adapter_or_llm_client"
        result_state["completed_nodes"] = completed
        result_state["current_node"] = NODE_REPAIR_FALLBACK
        result_state["repair_loop_count"] = new_repair_loops
        return result_state

    try:
        repair_result = await run_repair_subgraph(
            state,
            llm_client=llm_client,
            tool_adapter=adapter,
            ctx=ctx,
        )
    except Exception as exc:  # never let repair crash the main task
        logger.warning("repair subgraph crashed: %s", exc)
        # Best-effort: fall through to legacy regen
        result_state = await regenerate_sections_node(state, ctx=ctx)
        result_state["repair_fallback_reason"] = f"subgraph_exception:{type(exc).__name__}"
        result_state["completed_nodes"] = completed
        result_state["current_node"] = NODE_REPAIR_FALLBACK
        result_state["repair_loop_count"] = new_repair_loops
        return result_state

    # Map repair_result back into review_result so the routing layer
    # (which already reads review_result.level) can decide export.
    review_passed = bool(getattr(repair_result, "review_passed", False))
    updated_review = dict(review)
    updated_review["level"] = "passed" if review_passed else "failed"
    updated_review["repair_issues_resolved"] = list(getattr(repair_result, "issues_resolved", []) or [])
    updated_review["repair_issues_remaining"] = list(getattr(repair_result, "issues_remaining", []) or [])

    return {
        "review_result": updated_review,
        "repair_result": repair_result.model_dump() if hasattr(repair_result, "model_dump") else {},
        "repair_loop_count": new_repair_loops,
        "repair_fallback_reason": getattr(repair_result, "fallback_reason", None),
        "current_node": NODE_REPAIR_SUBGRAPH,
        "completed_nodes": completed,
    }


async def repair_fallback_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """Phase 2.4 — Repair Agent 失败兜底:字节级复用 ``regenerate_sections_node``。

    语义与 Phase 2.1 的 ``regenerate_sections`` 完全一致;只是从 repair_agent
    触发,作为 last-resort 修补,确保即使 Repair Agent 全部失败也能继续。
    """
    completed = mark_completed(state, NODE_REPAIR_FALLBACK)
    fallback_state = await regenerate_sections_node(state, ctx=ctx)
    fallback_state["repair_fallback_reason"] = fallback_state.get(
        "repair_fallback_reason"
    ) or "fallback_to_legacy_regen"
    fallback_state["completed_nodes"] = completed
    fallback_state["current_node"] = NODE_REPAIR_FALLBACK
    return fallback_state


__all__ = [
    "NODE_REVIEW_STEP",
    "NODE_REGENERATE_SECTIONS_STEP",
    "NODE_CHECK_FORMAT_STEP",
    "NODE_REPAIR_SUBGRAPH",
    "NODE_REPAIR_FALLBACK",
    "review_result_node",
    "regenerate_sections_node",
    "check_docx_format_node",
    "repair_subgraph_node",
    "repair_fallback_node",
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
