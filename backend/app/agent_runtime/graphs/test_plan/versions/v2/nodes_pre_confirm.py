"""v2 pre-confirm 节点 (Legacy orchestrator.run() pre-confirm 段)。

节点:
* ``initialize_task``              — 5 步 plan announce + status=planning/running
* ``validate_inputs``              — 校验文件上传;失败 → fail_task
* ``parse_requirement``            — 调 RequirementParserTool,写入 state.requirement_analysis
* ``parse_template``               — 调 TemplateParserTool + 装配 review_standard
* ``search_knowledge``             — KB 检索或 skip
* ``suggest_sections``             — SectionSuggestionTool
* ``pause_for_legacy_confirm``     — pause_marker=need_user_confirm,return to END
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from app.agent.enums import AgentEventType, TaskStatus

from app.agent_runtime.adapters.test_agent_tool_adapter import (
    TestAgentToolAdapter,
    UnknownToolError,
)
from app.agent_runtime.runtime_context import RuntimeContext

from app.agent_runtime.graphs.test_plan.constants import PLAN_STEP_VISIBLE_SECONDS as _PLAN_STEP_GAP
from app.agent_runtime.graphs.nodes_emit_helper import _emit_with_public_update
from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState
from .nodes_util import get_ctx, mark_completed

logger = logging.getLogger(__name__)

NODE_INITIALIZE_TASK = "initialize_task"
NODE_VALIDATE_INPUTS = "validate_inputs"
NODE_PARSE_REQUIREMENT = "parse_requirement"
NODE_PARSE_TEMPLATE = "parse_template"
NODE_SEARCH_KNOWLEDGE = "search_knowledge"
NODE_SUGGEST_SECTIONS = "suggest_sections"
NODE_PAUSE_FOR_LEGACY_CONFIRM = "pause_for_legacy_confirm"
# Phase 2.3: Preparation Agent dynamic subgraph nodes
NODE_PREP_SUBGRAPH = "prep_subgraph"
NODE_PREP_LEGACY_FALLBACK = "prep_legacy_fallback"


# ── helpers ──────────────────────────────────────────────────────────────────


async def _emit_plan_step(
    ctx: RuntimeContext, event_type: str, headline: str, payload: Dict[str, Any]
) -> None:
    """Emit a single plan-step event through the sink."""
    try:
        await ctx.event_sink.emit(
            task_id=str(ctx.task_internal_id),
            graph_run_id=f"run-{ctx.task_internal_id}",
            node_name=NODE_INITIALIZE_TASK,
            event_type=event_type,
            title=headline,
            content="",
            payload=payload,
        )
    except Exception as exc:
        logger.warning(
            "nodes_pre_confirm._emit_plan_step 失败 | task_id=%s | event_type=%s | err=%s",
            ctx.task_internal_id, event_type, exc,
        )
        raise


def _adapter(ctx: RuntimeContext) -> TestAgentToolAdapter | None:
    """节点从 ``ctx`` 读 adapter;若 ctx 没挂(测试)则回退 None。"""
    return getattr(ctx, "tool_adapter", None)


def _looks_like_pre_confirm_failure(envelope: Dict[str, Any]) -> bool:
    """CORE 工具调用失败(不可恢复)→ True。"""
    if envelope.get("success"):
        return False
    err = envelope.get("error") or {}
    if isinstance(err, dict) and err.get("recoverable") is False:
        return True
    if isinstance(err, dict) and err.get("code") in {
        "REQUIREMENT_FILE_NOT_FOUND",
        "TEMPLATE_FILE_NOT_FOUND",
        "REQUIREMENT_PARSE_FAILED",
        "TEMPLATE_PARSE_FAILED",
        "MISSING_REQUIREMENT",
        "MISSING_TEMPLATE",
        "INVALID_INPUTS",
    }:
        return True
    return False


# ── initialize_task ──────────────────────────────────────────────────────────


async def initialize_task_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """5 步 plan announce + status=running。"""
    logger.info("initialize_task_node: 进入节点 | task_id=%s", state.get("task_id"))
    completed = mark_completed(state, NODE_INITIALIZE_TASK)

    # 1) PLAN_STEP_STARTED (理解任务)
    await _emit_plan_step(
        ctx, AgentEventType.PLAN_STEP_STARTED.value,
        "正在理解任务",
        {"step": "understand", "status": "running"},
    )
    await asyncio.sleep(_PLAN_STEP_GAP)
    await _emit_plan_step(
        ctx, AgentEventType.PLAN_STEP_COMPLETED.value,
        "理解任务完成",
        {"step": "understand", "status": "done"},
    )
    await asyncio.sleep(_PLAN_STEP_GAP)

    # 2) PLAN_STEP_STARTED (生成执行计划)
    await _emit_plan_step(
        ctx, AgentEventType.PLAN_STEP_STARTED.value,
        "正在生成执行计划",
        {"step": "plan", "status": "running"},
    )
    await asyncio.sleep(_PLAN_STEP_GAP)
    await _emit_plan_step(
        ctx, AgentEventType.PLAN_STEP_COMPLETED.value,
        "生成执行计划完成",
        {"step": "plan", "status": "done"},
    )
    await asyncio.sleep(_PLAN_STEP_GAP)

    # 3) PLAN_CREATED(收尾 1 个事件)
    await _emit_plan_step(
        ctx, AgentEventType.PLAN_CREATED.value,
        "执行计划已生成",
        {"plan": {"steps": [
            "parse_requirement", "parse_template",
            "search_knowledge", "suggest_sections",
            "wait_user_confirm", "generate", "review", "export",
        ]}},
    )

    return {
        "task_status": TaskStatus.RUNNING.value,
        "current_node": NODE_INITIALIZE_TASK,
        "current_phase": "pre_confirm",
        "completed_nodes": completed,
    }


# ── validate_inputs ──────────────────────────────────────────────────────────


async def validate_inputs_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """校验最小可工作输入。失败 → ``last_error`` + status=failed。"""
    completed = mark_completed(state, NODE_VALIDATE_INPUTS)

    if not state.get("user_prompt") and not (state.get("requirement_file_id") or state.get("template_file_id")):
        return {
            "last_error": {
                "code": "INVALID_INPUTS",
                "message": "缺少 user_prompt 或 requirement_file_id/template_file_id",
            },
            "task_status": TaskStatus.FAILED.value,
            "current_node": NODE_VALIDATE_INPUTS,
            "current_phase": "failed",
            "completed_nodes": completed,
        }

    return {
        "last_error": None,
        "current_node": NODE_VALIDATE_INPUTS,
        "completed_nodes": completed,
    }


# ── parse_requirement ────────────────────────────────────────────────────────


async def parse_requirement_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """调 RequirementParserTool;失败按 CORE_TOOLS 规则 fail_task。"""
    completed = mark_completed(state, NODE_PARSE_REQUIREMENT)
    adapter = _adapter(ctx)
    if adapter is None:
        # 测试:若没注入 adapter,跳过工具调用,产出空 analysis
        return {
            "requirement_analysis": {},
            "current_node": NODE_PARSE_REQUIREMENT,
            "completed_nodes": completed,
        }

    requirement_file_id = state.get("requirement_file_id")
    if not requirement_file_id:
        return {
            "last_error": {"code": "MISSING_REQUIREMENT", "message": "缺少需求文件 ID"},
            "task_status": TaskStatus.FAILED.value,
            "current_node": NODE_PARSE_REQUIREMENT,
            "completed_nodes": completed,
        }

    envelope = await adapter.execute(
        tool_name="RequirementParserTool",
        inputs={"requirement_file_id": requirement_file_id},
        ctx_runtime=ctx,
    )

    if _looks_like_pre_confirm_failure(envelope):
        await ctx.event_sink.emit(
            task_id=str(ctx.task_internal_id),
            graph_run_id=f"run-{ctx.task_internal_id}",
            node_name=NODE_PARSE_REQUIREMENT,
            event_type=AgentEventType.TASK_FAILED.value,
            title="需求文档解析失败",
            content="",
            payload={"tool": "RequirementParserTool"},
        )
        return {
            "last_error": envelope.get("error") or {"code": "REQUIREMENT_PARSE_FAILED"},
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_PARSE_REQUIREMENT,
            "completed_nodes": completed,
        }

    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_PARSE_REQUIREMENT,
        event_type=AgentEventType.REQUIREMENT_SUMMARY.value,
        title="需求文档解析完成",
        content="",
        payload={},
    )

    return {
        "requirement_analysis": (envelope.get("data") or {}),
        "current_node": NODE_PARSE_REQUIREMENT,
        "completed_nodes": completed,
    }


# ── parse_template ───────────────────────────────────────────────────────────


async def parse_template_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """调 TemplateParserTool;数据写入 template_structure + review_standard。"""
    completed = mark_completed(state, NODE_PARSE_TEMPLATE)
    adapter = _adapter(ctx)
    if adapter is None:
        return {
            "template_structure": {},
            "review_standard": {},
            "current_node": NODE_PARSE_TEMPLATE,
            "completed_nodes": completed,
        }

    template_file_id = state.get("template_file_id")
    if not template_file_id:
        return {
            "last_error": {"code": "MISSING_TEMPLATE", "message": "缺少模板文件 ID"},
            "task_status": TaskStatus.FAILED.value,
            "current_node": NODE_PARSE_TEMPLATE,
            "completed_nodes": completed,
        }

    envelope = await adapter.execute(
        tool_name="TemplateParserTool",
        inputs={"template_file_id": template_file_id},
        ctx_runtime=ctx,
    )

    if _looks_like_pre_confirm_failure(envelope):
        return {
            "last_error": envelope.get("error") or {"code": "TEMPLATE_PARSE_FAILED"},
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_PARSE_TEMPLATE,
            "completed_nodes": completed,
        }

    data = envelope.get("data") or {}
    structure = data.get("template_structure") if isinstance(data.get("template_structure"), dict) else data
    review_standard = data.get("review_standard") or {}

    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_PARSE_TEMPLATE,
        event_type=AgentEventType.TEMPLATE_SUMMARY.value,
        title="模板解析完成",
        content="",
        payload={},
    )

    return {
        "template_structure": structure if isinstance(structure, dict) else {},
        "review_standard": review_standard if isinstance(review_standard, dict) else {},
        "current_node": NODE_PARSE_TEMPLATE,
        "completed_nodes": completed,
    }


# ── search_knowledge ─────────────────────────────────────────────────────────


async def search_knowledge_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """KB 检索:成功调 tool,失败 skip;最终写 knowledge_search_result。"""
    completed = mark_completed(state, NODE_SEARCH_KNOWLEDGE)
    adapter = _adapter(ctx)

    skip_reason = state.get("kb_skip_reason")

    if skip_reason:
        # skip 分支:不调 tool,synthetic TOOL_FINISHED + KNOWLEDGE_SUMMARY
        # Phase 2.8D:TOOL_FINISHED 走 _emit_with_public_update helper,嵌入 chunk 流
        await _emit_with_public_update(
            task_id=str(ctx.task_internal_id),
            node_name=NODE_SEARCH_KNOWLEDGE,
            event_type=AgentEventType.TOOL_FINISHED.value,
            payload={"tool_name": "KnowledgeSearchTool", "skipped": True, "skip_reason": skip_reason},
            ctx=ctx,
            tool_name="KnowledgeSearchTool",
            success=True,
            elapsed_ms=0,
        )
        await ctx.event_sink.emit(
            task_id=str(ctx.task_internal_id),
            graph_run_id=f"run-{ctx.task_internal_id}",
            node_name=NODE_SEARCH_KNOWLEDGE,
            event_type=AgentEventType.KNOWLEDGE_SUMMARY.value,
            title="知识库检索完成",
            content="",
            payload={"skipped": True, "reason": skip_reason},
        )
        return {
            "knowledge_search_result": {"skip_reason": skip_reason},
            "kb_skip_reason": skip_reason,
            "current_node": NODE_SEARCH_KNOWLEDGE,
            "completed_nodes": completed,
        }

    if adapter is None:
        return {
            "knowledge_search_result": {},
            "current_node": NODE_SEARCH_KNOWLEDGE,
            "completed_nodes": completed,
        }

    try:
        envelope = await adapter.execute(
            tool_name="KnowledgeSearchTool",
            inputs={},
            ctx_runtime=ctx,
        )
    except UnknownToolError:
        # 测试/stub:静默跳过
        envelope = {"success": True, "data": {}, "summary": "", "warnings": [], "error": None}

    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_SEARCH_KNOWLEDGE,
        event_type=AgentEventType.KNOWLEDGE_SUMMARY.value,
        title="知识库检索完成",
        content="",
        payload={},
    )

    return {
        "knowledge_search_result": (envelope.get("data") or {}),
        "current_node": NODE_SEARCH_KNOWLEDGE,
        "completed_nodes": completed,
    }


# ── suggest_sections ─────────────────────────────────────────────────────────


async def suggest_sections_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """调 SectionSuggestionTool;不 CORE 失败也不 fail_task(Legacy orchestrator 行为)。"""
    completed = mark_completed(state, NODE_SUGGEST_SECTIONS)
    adapter = _adapter(ctx)
    if adapter is None:
        return {
            "section_suggestions": {},
            "current_node": NODE_SUGGEST_SECTIONS,
            "completed_nodes": completed,
        }

    envelope = await adapter.execute(
        tool_name="SectionSuggestionTool",
        inputs={},
        ctx_runtime=ctx,
    )

    return {
        "section_suggestions": (envelope.get("data") or {}),
        "current_node": NODE_SUGGEST_SECTIONS,
        "completed_nodes": completed,
    }


# ── pause_for_legacy_confirm ─────────────────────────────────────────────────


async def pause_for_legacy_confirm_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """设置 pause_marker=need_user_confirm 并 exit to END。

    LangGraphRuntime 后续 ``ainvoke`` 返回后由 ``LangGraphRunCoordinator``
    检测该 marker。
    """
    completed = mark_completed(state, NODE_PAUSE_FOR_LEGACY_CONFIRM)

    section_suggestions = state.get("section_suggestions") or {}
    sections = section_suggestions.get("sections", []) if isinstance(section_suggestions, dict) else []
    logger.info(
        "pause_for_legacy_confirm_node: sections=%d | section_suggestions_keys=%s",
        len(sections), list(section_suggestions.keys()) if isinstance(section_suggestions, dict) else type(section_suggestions).__name__,
    )
    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_PAUSE_FOR_LEGACY_CONFIRM,
        event_type=AgentEventType.NEED_USER_CONFIRM.value,
        title="等待章节确认",
        content="",
        payload={"sections": sections, "confirmation_type": "section_generation_config"},
    )
    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_PAUSE_FOR_LEGACY_CONFIRM,
        event_type=AgentEventType.TASK_WAITING.value,
        title="任务等待用户确认",
        content="",
        payload={},
    )

    return {
        "pause_marker": "need_user_confirm",
        "current_phase": "paused",
        "task_status": TaskStatus.WAITING_USER_CONFIRM.value,
        "current_node": NODE_PAUSE_FOR_LEGACY_CONFIRM,
        "completed_nodes": completed,
    }


__all__ = [
    "NODE_INITIALIZE_TASK",
    "NODE_VALIDATE_INPUTS",
    "NODE_PARSE_REQUIREMENT",
    "NODE_PARSE_TEMPLATE",
    "NODE_SEARCH_KNOWLEDGE",
    "NODE_SUGGEST_SECTIONS",
    "NODE_PAUSE_FOR_LEGACY_CONFIRM",
    "NODE_PREP_SUBGRAPH",
    "NODE_PREP_LEGACY_FALLBACK",
    "initialize_task_node",
    "validate_inputs_node",
    "parse_requirement_node",
    "parse_template_node",
    "search_knowledge_node",
    "suggest_sections_node",
    "pause_for_legacy_confirm_node",
    # Phase 2.3
    "prep_subgraph_node",
    "prep_legacy_fallback_node",
]


# ── prep_subgraph (Phase 2.3) ──────────────────────────────────────────────


async def prep_subgraph_node(state: TestPlanGraphState, *, ctx) -> dict:
    """调 Preparation Agent 动态子图入口。

    * 仅当 ``preparation_agent_enabled=True`` 时被 route_after_parse_template 选中
    * 主入口 ``subgraph.run_preparation_subgraph`` 在 ainvoke 外执行
      (避免节点持有 LLMClient — Rule 10)
    * 把 result dump 写入 ``state.preparation_result`` + 审计 ``preparation_steps``
    """
    from app.agent_runtime.preparation.subgraph import (
        run_preparation_subgraph,
    )

    completed = mark_completed(state, NODE_PREP_SUBGRAPH)

    state_snapshot = {
        "user_prompt": state.get("user_prompt") or "",
        "requirement_summary": _summarize_requirement(state),
        "template_summary": _summarize_template(state),
        "knowledge_search_result": state.get("knowledge_search_result"),
        "kb_skip_reason": state.get("kb_skip_reason"),
        "completed_nodes": state.get("completed_nodes") or [],
    }

    # tool_adapter / llm_client 从 ctx 取(Phase 2.1 注入路径)
    tool_adapter = getattr(ctx, "tool_adapter", None)
    llm_client = getattr(ctx, "llm_client", None)
    if tool_adapter is None or llm_client is None:
        # 缺依赖 — 防御性 fallback 到 legacy single-shot KB
        logger.warning("prep_subgraph_node: missing tool_adapter/llm_client, falling back")
        from app.agent_runtime.preparation.fallback import run_legacy_kb_fallback
        fallback_data = await run_legacy_kb_fallback(state, ctx=ctx)
        return {
            **fallback_data,
            "current_node": NODE_PREP_SUBGRAPH,
            "completed_nodes": completed,
            "preparation_result": None,
            "preparation_fallback_reason": "missing_runtime_dependencies",
        }

    result = await run_preparation_subgraph(
        state_snapshot,
        llm_client=llm_client,
        tool_adapter=tool_adapter,
        ctx=ctx,
    )

    return {
        "preparation_result": result.model_dump(),
        "preparation_steps": list(state.get("preparation_steps") or []) + [
            # 第一行 audit 标记本次 prep 已结束;后续由 agent_loop 在步骤内填
            {
                "decision_summary": "preparation_subgraph_completed",
                "action": "finish" if result.information_sufficient else "fail",
                "tool_name": "",
                "args_signature": "",
                "outcome": "ok" if not result.fallback_reason else "fallback",
            }
        ],
        "preparation_budget_state": result.budget_state.model_dump(),
        "knowledge_search_result": (
            state.get("knowledge_search_result")
            or {"preparation_evidence_count": len(result.evidence)}
        ),
        "current_node": NODE_PREP_SUBGRAPH,
        "completed_nodes": completed,
    }


async def prep_legacy_fallback_node(state: TestPlanGraphState, *, ctx) -> dict:
    """Prep 触发 fallback 时(不可恢复错误)的统一入口,字节级复用 legacy KB 主体。

    严格镜像 nodes_pre_confirm.search_knowledge_node 主体。
    """
    from app.agent_runtime.preparation.fallback import run_legacy_kb_fallback
    fallback_data = await run_legacy_kb_fallback(state, ctx=ctx)
    completed = mark_completed(state, NODE_PREP_LEGACY_FALLBACK)
    return {
        **fallback_data,
        "current_node": NODE_PREP_LEGACY_FALLBACK,
        "completed_nodes": completed,
        "preparation_fallback_reason": "fallback_invoked",
    }


# ── helpers ────────────────────────────────────────────────────────────────


def _summarize_requirement(state: TestPlanGraphState) -> str:
    """Mirror search_knowledge / prompt 期望的 requirement_summary 字段。

    priority: state["requirement_summary"] > str(state["requirement_analysis"])[:4096]
    """
    explicit = state.get("requirement_summary")
    if explicit:
        return str(explicit)[:4096]
    analysis = state.get("requirement_analysis") or {}
    if isinstance(analysis, dict):
        # 取最长文本字段
        best = ""
        for _k, v in analysis.items():
            if isinstance(v, str) and len(v) > len(best):
                best = v
        return best[:4096]
    return ""


def _summarize_template(state: TestPlanGraphState) -> str:
    explicit = state.get("template_summary")
    if explicit:
        return str(explicit)[:2048]
    tpl = state.get("template_structure") or {}
    if isinstance(tpl, dict):
        # 取 template_structure.summary 或顶层 section names
        if isinstance(tpl.get("summary"), str):
            return tpl["summary"][:2048]
        sections = tpl.get("sections")
        if isinstance(sections, list):
            return ", ".join(
                str(s.get("name") or s.get("title") or "") for s in sections
            )[:2048]
    return ""

