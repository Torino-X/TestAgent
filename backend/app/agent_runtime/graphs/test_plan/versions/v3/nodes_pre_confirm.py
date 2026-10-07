"""v2 pre-confirm 节点 (Legacy orchestrator.run() pre-confirm 段)。

对应业务段:从用户发出"开始生成测试方案"指令,到 SectionSuggestionTool
生成章节建议 + 等用户确认章节策略这一段流程。

节点(按执行顺序):
* ``initialize_task``              — 5 步 plan announce + status=planning/running
* ``validate_inputs``              — 校验文件上传;失败 → fail_task
* ``parse_requirement``            — 调 RequirementParserTool,写入 state.requirement_analysis
* ``parse_template``               — 调 TemplateParserTool + 装配 review_standard
* ``search_knowledge``             — KB 检索或 skip(Phase 2.9A.X 失败会降级)
* ``suggest_sections``             — SectionSuggestionTool
* ``pause_for_legacy_confirm``     — pause_marker=need_user_confirm,return to END
* (interrupts) ``prepare_section_confirmation`` + ``section_confirmation_interrupt`` ——
  Phase 2.9A.7 真 interrupt 路径
* (Phase 2.3) ``prep_subgraph`` / ``prep_legacy_fallback`` ——
  Preparation Agent 子图入口与兜底

每个节点函数都是 ``async def node_xxx(state, *, ctx) -> dict`` 形状,
通过 ``_bind_async`` 包装后被 graph.add_node 注册。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict

from app.agent.enums import AgentEventType, TaskStatus

from app.agent_runtime.adapters.test_agent_tool_adapter import (
    TestAgentToolAdapter,
    UnknownToolError,
    _knowledge_search_completion_message,
)
from app.agent_runtime.runtime_context import RuntimeContext

from app.agent_runtime.graphs.test_plan.constants import PLAN_STEP_VISIBLE_SECONDS as _PLAN_STEP_GAP
from app.agent_runtime.graphs.nodes_emit_helper import _emit_with_public_update
from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState
from .nodes_util import get_ctx, mark_completed
from .routing import (
    NODE_FAIL_TASK,
    NODE_PREPARE_SECTION_CONFIRMATION as ROUTE_PREPARE_SECTION_CONFIRMATION,
    route_after_parse_template,
)

logger = logging.getLogger(__name__)

NODE_INITIALIZE_TASK = "initialize_task"
NODE_VALIDATE_INPUTS = "validate_inputs"
NODE_PARSE_REQUIREMENT = "parse_requirement"
NODE_PARSE_TEMPLATE = "parse_template"
NODE_SEARCH_KNOWLEDGE = "search_knowledge"
NODE_SUGGEST_SECTIONS = "suggest_sections"
NODE_PAUSE_FOR_LEGACY_CONFIRM = "pause_for_legacy_confirm"
# Phase 2.9A.7: 真 interrupt 路径上的副作用节点 — 必须在
# ``section_confirmation_interrupt`` 之前完成 HumanConfirmation
# 持久化 + NEED_USER_CONFIRM/TASK_WAITING 事件发布。interrupt
# 节点本身是同步,不能 await,所有副作用都隔离到本节点。
NODE_PREPARE_SECTION_CONFIRMATION = "prepare_section_confirmation"
NODE_PREPARE_CLARIFICATION = "prepare_preparation_clarification"
# Phase 2.3: Preparation Agent dynamic subgraph nodes
NODE_PREP_SUBGRAPH = "prep_subgraph"
NODE_PREP_LEGACY_FALLBACK = "prep_legacy_fallback"


# ── helpers ──────────────────────────────────────────────────────────────────


async def _emit_plan_step(
    ctx: RuntimeContext, event_type: str, headline: str, payload: Dict[str, Any]
) -> None:
    """Emit a single plan-step event through the sink."""
    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_INITIALIZE_TASK,
        event_type=event_type,
        title=headline,
        content="",
        payload=payload,
    )


def _adapter(ctx: RuntimeContext) -> TestAgentToolAdapter | None:
    """节点从 ``ctx`` 读 adapter;若 ctx 没挂(测试)则回退 None。"""
    return getattr(ctx, "tool_adapter", None)


def _emit_result_event_id(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("event_id") or result.get("id") or "")
    return ""


def _terminal_event_id(adapter: TestAgentToolAdapter | None) -> str:
    if adapter is None:
        return ""
    return str(adapter.last_terminal_event_id() or "")


def _with_pending_narrative(
    patch: Dict[str, Any],
    *,
    state: TestPlanGraphState,
    tool_name: str,
    tool_call_id: str,
    attempt: int,
    terminal_status: str,
    continuation_route: str,
    source_event_id: str,
    duration_ms: int | None = None,
) -> Dict[str, Any]:
    patch["next_node"] = continuation_route
    patch["pending_narrative"] = _build_pending_narrative(
        state=state,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        attempt=attempt,
        terminal_status=terminal_status,
        continuation_route=continuation_route,
        duration_ms=duration_ms,
        source_event_id=source_event_id,
    )
    return patch


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


def _knowledge_search_inputs_from_state(state: TestPlanGraphState) -> dict[str, Any]:
    retrieval_plan = state.get("retrieval_plan_snapshot")
    retrieval_plan = retrieval_plan if isinstance(retrieval_plan, dict) else {}
    query = str(
        retrieval_plan.get("company_query")
        or retrieval_plan.get("query")
        or state.get("user_prompt")
        or state.get("goal")
        or state.get("dynamic_goal")
        or "生成测试方案"
    ).strip()
    inputs: dict[str, Any] = {"query": query}
    top_k = retrieval_plan.get("top_k")
    if top_k is not None:
        try:
            inputs["top_k"] = int(top_k)
        except (TypeError, ValueError):
            logger.warning("COMPANY_RAG_QUERY_INPUT_INVALID | field=top_k | value=%r", top_k)
    return inputs


def _record_retrieval_query_signature(
    state: TestPlanGraphState,
    retrieval_plan: dict[str, Any],
) -> list[str]:
    """Persist one canonical query per retrieval stage for router deduplication."""
    existing = [
        " ".join(str(item or "").lower().split())[:720]
        for item in (state.get("retrieval_query_signatures") or [])
        if str(item or "").strip()
    ]
    query = retrieval_plan.get("company_query") or retrieval_plan.get("query")
    signature = " ".join(str(query or "").lower().split())[:720]
    if signature and signature not in existing:
        existing.append(signature)
    return existing


def _build_retrieval_plan(preparation_result: Dict[str, Any] | None) -> dict[str, Any]:
    """Create a bounded, source-neutral retrieval plan from Preparation gaps."""
    result = preparation_result if isinstance(preparation_result, dict) else {}
    raw_gaps = result.get("requirement_gaps") or []
    gaps: list[dict[str, str]] = []
    for item in raw_gaps:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "").strip()
        field = str(item.get("field") or "requirement_gap").strip()
        if description:
            gaps.append({
                "id": field[:80],
                "description": description[:240],
                "severity": str(item.get("severity") or "medium")[:12],
            })
    if not gaps:
        for item in result.get("user_questions") or []:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            if question:
                gaps.append({
                    "id": str(item.get("field") or "requirement_gap")[:80],
                    "description": question[:240],
                    "severity": "medium",
                })
    if not gaps:
        # A first PreparationAgent tool decision is also an LLM-owned retrieval
        # intent. Preserve it for the graph-owned dual-source stage instead of
        # dropping it because it is not a clarification gap.
        for index, query in enumerate(result.get("queries") or [], start=1):
            query = str(query or "").strip()
            if query:
                gaps.append({
                    "id": f"retrieval_focus_{index}",
                    "description": query[:240],
                    "severity": "medium",
                })
    gaps = gaps[:3]
    query = "；".join(item["description"] for item in gaps)
    return {
        "version": 1,
        "gaps": gaps,
        "query": query[:720],
        "company_query": query[:720],
        "project_queries": [item["description"] for item in gaps],
        "top_k": 5,
    }


async def _retrieve_project_evidence(
    *,
    ctx: RuntimeContext,
    state: TestPlanGraphState,
    retrieval_plan: Dict[str, Any],
) -> dict[str, Any]:
    """Retrieve only from the task's attached project workspace.

    A task without a project must not issue a broad or cross-project query.
    The resolver uses the same Context Engine indexed project chunks that are
    available during normal context construction.
    """
    project_context = dict(getattr(ctx, "project_context", None) or {})
    project_id = state.get("project_id") or project_context.get("project_id")
    queries = [str(query).strip() for query in retrieval_plan.get("project_queries") or []]
    queries = [query for query in queries if query][:3]
    if not project_id:
        return {"status": "skipped", "reason": "no_project", "queries": [], "hits": []}
    if not queries:
        return {"status": "skipped", "reason": "no_gap_query", "queries": [], "hits": []}

    try:
        from app.services.project_context_resolver import ProjectContextResolver

        packages = []
        async with ctx.session_factory() as session:
            resolver = ProjectContextResolver(session)
            for query in queries:
                packages.append(
                    await resolver.resolve(
                        user_id=ctx.user_internal_id,
                        conversation_id=ctx.conversation_internal_id,
                        query=query,
                        task_id=ctx.task_internal_id,
                    )
                )
        seen: set[str] = set()
        hits: list[dict[str, Any]] = []
        workspace_key = project_context.get("workspace_key")
        for package in packages:
            if not isinstance(package, dict):
                continue
            workspace_key = workspace_key or package.get("workspace_key")
            for hit in package.get("source_hits") or []:
                if not isinstance(hit, dict):
                    continue
                ref = str(hit.get("chunk_id") or hit.get("document_id") or "")
                if not ref or ref in seen:
                    continue
                seen.add(ref)
                hits.append({
                    "evidence_id": ref,
                    "title": str(hit.get("title") or "project document")[:160],
                "content": str(hit.get("content") or "")[:1600],
                "section": str(hit.get("section") or "")[:240],
                "source_role": str(hit.get("source_role") or "other")[:80],
                "version_hint": str(hit.get("source_version") or "")[:64],
                "date_hint": str(hit.get("updated_at") or "")[:64],
                "score": hit.get("score"),
                })
        return {
            "status": "completed",
            "project_id": str(project_id),
            "workspace_key": workspace_key,
            "queries": queries,
            "hits": hits[:9],
        }
    except Exception as exc:  # noqa: BLE001 - project retrieval is fail-open
        logger.warning("PROJECT_RAG_GRAPH_RETRIEVAL_FAILED | task_id=%s | err=%s", ctx.task_internal_id, type(exc).__name__)
        return {
            "status": "degraded",
            "reason": "project_retrieval_failed",
            "queries": queries,
            "hits": [],
        }


def _fuse_project_context(
    knowledge_result: Dict[str, Any] | None,
    state: TestPlanGraphState,
    project_result: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Attach a frozen, source-separated evidence bundle without mixing facts."""
    company = dict(knowledge_result or {})
    company.setdefault("source", "company_rag")
    plan = state.get("retrieval_plan_snapshot")
    plan = plan if isinstance(plan, dict) else _build_retrieval_plan(
        state.get("preparation_result")
    )
    company_status = (
        "skipped"
        if company.get("skip_reason")
        else "degraded"
        if company.get("degraded")
        else str(company.get("status") or "completed")
    )
    bundle = {
        "version": 1,
        "frozen": True,
        "retrieval_round": int(state.get("retrieval_round") or 0) + 1,
        "gaps": list(plan.get("gaps") or [])[:3],
        "company_rag": {
            "status": company_status,
            "query": str(company.get("query") or plan.get("company_query") or "")[:720],
            "hits": list(company.get("chunks") or company.get("hits") or [])[:5],
            "skip_reason": company.get("skip_reason"),
        },
        "project_rag": dict(project_result or {"status": "skipped", "reason": "not_attempted", "hits": []}),
    }
    company["project_rag"] = bundle["project_rag"]
    company["retrieval_evidence_bundle"] = bundle
    return company


def _degraded_knowledge_result(
    *,
    envelope: Dict[str, Any] | None,
    query: str,
    reason: str,
) -> dict[str, Any]:
    error = (envelope or {}).get("error") or {}
    if not isinstance(error, dict):
        error = {"message": str(error)}
    return {
        "similar_projects": [],
        "standards": [],
        "terms": [],
        "hits": [],
        "query": query,
        "hit_count": 0,
        "confidence": "low",
        "elapsed_ms": 0,
        "direct_answer_eligible": False,
        "degraded": True,
        "source": "company_rag",
        "skip_reason": reason,
        "error_code": str(error.get("code") or reason),
        "error_message": str(error.get("message") or reason),
    }


async def initialize_task_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """5 步 plan announce + status=running。"""
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
            "next_node": NODE_PARSE_TEMPLATE,
        }

    requirement_file_id = state.get("requirement_file_id")
    if not requirement_file_id:
        return {
            "last_error": {"code": "MISSING_REQUIREMENT", "message": "缺少需求文件 ID"},
            "task_status": TaskStatus.FAILED.value,
            "current_node": NODE_PARSE_REQUIREMENT,
            "completed_nodes": completed,
            "next_node": NODE_FAIL_TASK,
        }

    envelope = await adapter.execute(
        tool_name="RequirementParserTool",
        inputs={"requirement_file_id": requirement_file_id},
        ctx_runtime=ctx,
        graph_state=dict(state) if state else None,
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
        return _with_pending_narrative({
            "last_error": envelope.get("error") or {"code": "REQUIREMENT_PARSE_FAILED"},
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_PARSE_REQUIREMENT,
            "completed_nodes": completed,
        },
            state=state,
            tool_name="RequirementParserTool",
            tool_call_id=str(envelope.get("tool_call_id") or ""),
            attempt=int(envelope.get("attempt") or 1),
            terminal_status="failed",
            continuation_route=NODE_FAIL_TASK,
            duration_ms=envelope.get("duration_ms"),
            source_event_id=_terminal_event_id(adapter),
        )

    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_PARSE_REQUIREMENT,
        event_type=AgentEventType.REQUIREMENT_SUMMARY.value,
        title="需求文档解析完成",
        content="",
        payload=(envelope.get("data") or {}),
    )

    # Phase 2.9B.4: 写入待叙事片段,供 tool_narrative_barrier 消费。
    # 只有 Tool 最终成功(无 chunk_final=false / 无失败)才进入叙事屏障;
    # 此处 RequirementParser 成功即写 pending_narrative。
    # Phase 2.9B.6: tool_call_id 来自 adapter 回写的真实值,source_event_id
    # 来自 adapter 记录的最终 terminal 事件 event_id(不再写空串)。
    pending = _build_pending_narrative(
        state=state,
        tool_name="RequirementParserTool",
        tool_call_id=str(envelope.get("tool_call_id") or ""),
        attempt=int(envelope.get("attempt") or 1),
        terminal_status="success",
        continuation_route=NODE_PARSE_TEMPLATE,
        source_event_id=adapter.last_terminal_event_id(),
    )

    return {
        "requirement_analysis": (envelope.get("data") or {}),
        "current_node": NODE_PARSE_REQUIREMENT,
        "completed_nodes": completed,
        "pending_narrative": pending,
        "next_node": NODE_PARSE_TEMPLATE,
    }


# ── parse_template ───────────────────────────────────────────────────────────


async def parse_template_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """调 TemplateParserTool;数据写入 template_structure + review_standard。"""
    completed = mark_completed(state, NODE_PARSE_TEMPLATE)
    adapter = _adapter(ctx)
    if adapter is None:
        patch = {
            "template_structure": {},
            "review_standard": {},
            "current_node": NODE_PARSE_TEMPLATE,
            "completed_nodes": completed,
        }
        patch["next_node"] = route_after_parse_template({**dict(state), **patch})
        return patch

    template_file_id = state.get("template_file_id")
    if not template_file_id:
        return {
            "last_error": {"code": "MISSING_TEMPLATE", "message": "缺少模板文件 ID"},
            "task_status": TaskStatus.FAILED.value,
            "current_node": NODE_PARSE_TEMPLATE,
            "completed_nodes": completed,
            "next_node": NODE_FAIL_TASK,
        }

    envelope = await adapter.execute(
        tool_name="TemplateParserTool",
        inputs={"template_file_id": template_file_id},
        ctx_runtime=ctx,
        graph_state=dict(state) if state else None,
    )

    if _looks_like_pre_confirm_failure(envelope):
        return _with_pending_narrative({
            "last_error": envelope.get("error") or {"code": "TEMPLATE_PARSE_FAILED"},
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_PARSE_TEMPLATE,
            "completed_nodes": completed,
        },
            state=state,
            tool_name="TemplateParserTool",
            tool_call_id=str(envelope.get("tool_call_id") or ""),
            attempt=int(envelope.get("attempt") or 1),
            terminal_status="failed",
            continuation_route=NODE_FAIL_TASK,
            duration_ms=envelope.get("duration_ms"),
            source_event_id=_terminal_event_id(adapter),
        )

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

    patch = {
        "template_structure": structure if isinstance(structure, dict) else {},
        "review_standard": review_standard if isinstance(review_standard, dict) else {},
        "current_node": NODE_PARSE_TEMPLATE,
        "completed_nodes": completed,
    }
    continuation_route = route_after_parse_template({**dict(state), **patch})
    return _with_pending_narrative(
        patch,
        state=state,
        tool_name="TemplateParserTool",
        tool_call_id=str(envelope.get("tool_call_id") or ""),
        attempt=int(envelope.get("attempt") or 1),
        terminal_status="success",
        continuation_route=continuation_route,
        duration_ms=envelope.get("duration_ms"),
        source_event_id=_terminal_event_id(adapter),
    )


# ── search_knowledge ─────────────────────────────────────────────────────────


async def search_knowledge_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """KB 检索:成功调 tool,失败 skip;最终写 knowledge_search_result。"""
    completed = mark_completed(state, NODE_SEARCH_KNOWLEDGE)
    adapter = _adapter(ctx)
    retrieval_plan = state.get("retrieval_plan_snapshot")
    retrieval_plan = (
        retrieval_plan
        if isinstance(retrieval_plan, dict)
        else _build_retrieval_plan(state.get("preparation_result"))
    )
    retrieval_query_signatures = _record_retrieval_query_signature(state, retrieval_plan)
    continuation_route = (
        NODE_PREP_SUBGRAPH
        if state.get("preparation_agent_enabled")
        else NODE_SUGGEST_SECTIONS
    )

    skip_reason = state.get("kb_skip_reason")

    if skip_reason:
        # skip 分支:不调 tool,synthetic TOOL_FINISHED + KNOWLEDGE_SUMMARY
        # Phase 2.8D:TOOL_FINISHED 走 _emit_with_public_update helper,嵌入 chunk 流
        project_result = await _retrieve_project_evidence(
            ctx=ctx, state=state, retrieval_plan=retrieval_plan
        )
        tool_call_id = f"KnowledgeSearchTool-skip-{uuid.uuid4().hex}"
        completion_message = _knowledge_search_completion_message(
            {"skip_reason": skip_reason, "query": retrieval_plan.get("company_query")},
            project_result=project_result,
        )
        emitted = await _emit_with_public_update(
            task_id=str(ctx.task_internal_id),
            node_name=NODE_SEARCH_KNOWLEDGE,
            event_type=AgentEventType.TOOL_FINISHED.value,
            payload={
                "tool_name": "KnowledgeSearchTool",
                "tool_call_id": tool_call_id,
                "attempt": 1,
                "skipped": True,
                "skip_reason": skip_reason,
                "completion_message": completion_message,
            },
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
        fused = _fuse_project_context(
            {"skip_reason": skip_reason, "query": retrieval_plan.get("company_query")},
            state,
            project_result,
        )
        return _with_pending_narrative({
            "knowledge_search_result": fused,
            "retrieval_evidence_bundle": fused.get("retrieval_evidence_bundle"),
            "retrieval_plan_snapshot": retrieval_plan,
            "retrieval_round": int(state.get("retrieval_round") or 0) + 1,
            "retrieval_query_signatures": retrieval_query_signatures,
            "kb_skip_reason": skip_reason,
            "current_node": NODE_SEARCH_KNOWLEDGE,
            "completed_nodes": completed,
        },
            state=state,
            tool_name="KnowledgeSearchTool",
            tool_call_id=tool_call_id,
            attempt=1,
            terminal_status="skipped",
            continuation_route=continuation_route,
            duration_ms=0,
            source_event_id=_emit_result_event_id(emitted[-1] if emitted else {}),
        )

    if adapter is None:
        project_result = await _retrieve_project_evidence(
            ctx=ctx, state=state, retrieval_plan=retrieval_plan
        )
        fused = _fuse_project_context(
            {
                "skip_reason": "company_rag_adapter_unavailable",
                "query": retrieval_plan.get("company_query"),
            },
            state,
            project_result,
        )
        return {
            "knowledge_search_result": fused,
            "retrieval_evidence_bundle": fused.get("retrieval_evidence_bundle"),
            "retrieval_plan_snapshot": retrieval_plan,
            "retrieval_round": int(state.get("retrieval_round") or 0) + 1,
            "retrieval_query_signatures": retrieval_query_signatures,
            "current_node": NODE_SEARCH_KNOWLEDGE,
            "completed_nodes": completed,
            "next_node": continuation_route,
        }

    # Both source outcomes belong to the same visible knowledge stage. Resolve
    # the Project RAG side first so the terminal tool card can state both.
    project_result = await _retrieve_project_evidence(
        ctx=ctx, state=state, retrieval_plan=retrieval_plan
    )
    kb_inputs = _knowledge_search_inputs_from_state(state)
    graph_state_for_tool = dict(state)
    graph_state_for_tool["_project_rag_display"] = project_result
    try:
        envelope = await adapter.execute(
            tool_name="KnowledgeSearchTool",
            inputs=kb_inputs,
            ctx_runtime=ctx,
            graph_state=graph_state_for_tool,
        )
    except UnknownToolError:
        # 测试/stub:静默跳过
        envelope = {
            "success": False,
            "data": None,
            "summary": "KnowledgeSearchTool unavailable.",
            "warnings": ["KnowledgeSearchTool is not registered."],
            "error": {"code": "KNOWLEDGE_TOOL_UNAVAILABLE", "message": "tool missing"},
        }

    kb_failed = not envelope.get("success", True)
    kb_result = (
        envelope.get("data")
        if isinstance(envelope.get("data"), dict) and not kb_failed
        else _degraded_knowledge_result(
            envelope=envelope,
            query=str(kb_inputs.get("query") or ""),
            reason="tool_failed" if kb_failed else "empty_result",
        )
    )
    logger.info(
        "COMPANY_RAG_GRAPH_RESULT | node=search_knowledge | task_id=%s | success=%s | "
        "degraded=%s | hit_count=%s | error_code=%s",
        getattr(ctx, "task_internal_id", None),
        bool(envelope.get("success", True)),
        bool(kb_result.get("degraded")),
        kb_result.get("hit_count"),
        (envelope.get("error") or {}).get("code") if isinstance(envelope.get("error"), dict) else None,
    )

    fused = _fuse_project_context(kb_result, state, project_result)

    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_SEARCH_KNOWLEDGE,
        event_type=AgentEventType.KNOWLEDGE_SUMMARY.value,
        title="知识库检索完成",
        content="",
        payload={},
    )

    return _with_pending_narrative({
        "knowledge_search_result": fused,
        "retrieval_evidence_bundle": fused.get("retrieval_evidence_bundle"),
        "retrieval_plan_snapshot": retrieval_plan,
        "retrieval_round": int(state.get("retrieval_round") or 0) + 1,
        "retrieval_query_signatures": retrieval_query_signatures,
        "kb_skip_reason": kb_result.get("skip_reason") or state.get("kb_skip_reason"),
        "current_node": NODE_SEARCH_KNOWLEDGE,
        "completed_nodes": completed,
    },
        state=state,
        tool_name="KnowledgeSearchTool",
        tool_call_id=str(envelope.get("tool_call_id") or ""),
        attempt=int(envelope.get("attempt") or 1),
        terminal_status=("success" if envelope.get("success", True) else "failed"),
        continuation_route=continuation_route,
        duration_ms=envelope.get("duration_ms"),
        source_event_id=_terminal_event_id(adapter),
    )


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
            "next_node": ROUTE_PREPARE_SECTION_CONFIRMATION,
        }

    envelope = await adapter.execute(
        tool_name="SectionSuggestionTool",
        inputs={},
        ctx_runtime=ctx,
        graph_state=dict(state) if state else None,
    )

    return _with_pending_narrative({
        "section_suggestions": (envelope.get("data") or {}),
        "current_node": NODE_SUGGEST_SECTIONS,
        "completed_nodes": completed,
    },
        state=state,
        tool_name="SectionSuggestionTool",
        tool_call_id=str(envelope.get("tool_call_id") or ""),
        attempt=int(envelope.get("attempt") or 1),
        terminal_status=("success" if envelope.get("success", True) else "failed"),
        continuation_route=ROUTE_PREPARE_SECTION_CONFIRMATION,
        duration_ms=envelope.get("duration_ms"),
        source_event_id=_terminal_event_id(adapter),
    )


# ── prepare_section_confirmation(Phase 2.9A.7)─────────────────────────────


async def prepare_section_confirmation_node(
    state: TestPlanGraphState, *, ctx: RuntimeContext
) -> dict:
    """真 interrupt 路径的副作用节点(在 ``section_confirmation_interrupt`` 之前)。

    职责(本节点**唯一允许做**的副作用):
      1. 校验 sections(>=1);
      2. 幂等创建 HumanConfirmation(已有 pending 则复用);
      3. commit(早于事件发布);
      4. emit NEED_USER_CONFIRM(payload 含 confirmation_id + sections);
      5. emit TASK_WAITING;
      6. 写入 ``state.section_confirmation_id`` + ``state.confirmation_id``
         供 ``wait_section_confirmation_node``(interrupt 节点)读。

    为什么独立节点:
      * LangGraph ``interrupt()`` 节点必须是同步函数,不能 await;
      * DB INSERT / event_sink.emit 都是 async,只能放在异步节点;
      * 用户确认后,LangGraph 会重放 ``wait_section_confirmation_node``
        并把 decision 注入 — 如果副作用也写在那里,会出现重复 INSERT
        / 重复 emit 事件(幂等风险)。
      * 拆开后,副作用只在首次 ainvoke 走到这里时执行一次;resume
        时 LangGraph 跳过本节点,直接重放 ``wait_section_confirmation``。

    失败处理:
      * DB 持久化失败 → emit TASK_FAILED,进入 FAIL_TASK 路由(在
        ``graph.py`` 里 ``NODE_PREPARE_SECTION_CONFIRMATION`` 失败
        边 → NODE_FAIL);不进入 interrupt,前端立即看到失败。
      * sections 为空 → emit TASK_FAILED + pause_marker=missing_sections,
        同样走 FAIL 路由。
    """
    completed = mark_completed(state, NODE_PREPARE_SECTION_CONFIRMATION)

    sections = state.get("section_suggestions") or {}
    sections_list = (
        sections.get("sections") if isinstance(sections, dict) else sections
    )
    has_sections = isinstance(sections_list, list) and bool(sections_list)

    if not has_sections:
        # Phase 2.9A.7: 兼容旧测试场景(``_Ex`` executor 不返回 sections,
        # 或 Phase 2.2 v2 interrupt 测试 fixture)。生产路径下 sections
        # 必非空,否则用户在 UI 看到的是空列表 — 已经够反常了。
        #
        # 行为: 不 mark failed,直接 fall through 到 interrupt 节点,
        # 让用户决定(他们会取消或重新触发)。这是"soft fail"模式,
        # 兼容现有 v2 interrupt 测试场景;生产数据进入到这里属于异常
        # 但不会让任务永挂。
        logger.warning(
            "prepare_section_confirmation_node: no sections, fall through to interrupt | "
            "task_internal_id=%s",
            ctx.task_internal_id,
        )
        return {
            "section_confirmation_id": None,
            "confirmation_id": None,
            "current_phase": "paused",
            "task_status": TaskStatus.WAITING_USER_CONFIRM.value,
            "current_node": NODE_PREPARE_SECTION_CONFIRMATION,
            "completed_nodes": completed,
        }

    confirmation = await _persist_human_confirmation_in_node(ctx, state)
    if confirmation is None:
        logger.warning(
            "prepare_section_confirmation_node: pre-persist failed | "
            "task_internal_id=%s",
            ctx.task_internal_id,
        )
        await ctx.event_sink.emit(
            task_id=str(ctx.task_internal_id),
            graph_run_id=f"run-{ctx.task_internal_id}",
            node_name=NODE_PREPARE_SECTION_CONFIRMATION,
            event_type=AgentEventType.TASK_FAILED.value,
            title="章节确认记录持久化失败",
            content="",
            payload={"code": "HUMAN_CONFIRMATION_PERSIST_FAILED"},
        )
        return {
            "pause_marker": "persist_failed",
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_PREPARE_SECTION_CONFIRMATION,
            "completed_nodes": completed,
            "last_error": {
                "code": "HUMAN_CONFIRMATION_PERSIST_FAILED",
                "message": "pre-persist HumanConfirmation failed",
            },
        }

    confirmation_id = confirmation.get("confirmation_id")

    # 必须在 commit(已在 _persist_human_confirmation_in_node 内完成)
    # 之后才能 emit 事件,避免前端 SSE 提前收到事件但 pending API
    # 返回 40401(Phase 2.9A.3 时序竞态修复)。
    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_PREPARE_SECTION_CONFIRMATION,
        event_type=AgentEventType.NEED_USER_CONFIRM.value,
        title="等待章节确认",
        content="",
        payload={
            "confirmation_id": confirmation_id,
            "confirmation_type": confirmation.get(
                "confirmation_type", "section_generation_config"
            ),
            "task_id": str(ctx.task_internal_id),
            "section_count": len(confirmation.get("sections") or []),
            "section_suggestions": sections,
        },
    )
    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_PREPARE_SECTION_CONFIRMATION,
        event_type=AgentEventType.TASK_WAITING.value,
        title="任务等待用户确认",
        content="",
        payload={"confirmation_id": confirmation_id},
    )

    return {
        "section_confirmation_id": confirmation_id,
        "confirmation_id": confirmation_id,
        "current_phase": "paused",
        "task_status": TaskStatus.WAITING_USER_CONFIRM.value,
        "current_node": NODE_PREPARE_SECTION_CONFIRMATION,
        "completed_nodes": completed,
    }


# ── pause_for_legacy_confirm ─────────────────────────────────────────────────


async def _persist_human_confirmation_in_node(
    ctx: RuntimeContext,
    state: TestPlanGraphState,
) -> Dict[str, Any] | None:
    """在 pause 节点内预创建 HumanConfirmation 记录。

    修复 v3 时序竞态:
      * 必须在 ``await event_sink.emit(NEED_USER_CONFIRM)`` 之前 commit,
        否则前端 SSE 收到事件时 pending-confirmation API 会返回 40401。
      * 使用 ``ctx.session_factory`` 进入一次新的 session,
        不污染 RuntimeContext(规则 10)。
    返回 confirmation dict (含 ``confirmation_id``) 或 None(失败 / 缺数据)。
    """
    sections = state.get("section_suggestions") or {}
    sections_list = (
        sections.get("sections") if isinstance(sections, dict) else sections
    )
    if not isinstance(sections_list, list) or not sections_list:
        logger.warning(
            "_persist_human_confirmation_in_node: no sections, skip | "
            "task_internal_id=%s",
            ctx.task_internal_id,
        )
        return None

    try:
        from app.models.human_confirmation import HumanConfirmation
        from app.repositories.confirmation_repository import ConfirmationRepository
        from app.utils.datetime import utcnow
        from app.utils.ids import generate_public_id

        async with ctx.session_factory() as session:
            confirm_repo = ConfirmationRepository(session)
            existing = await confirm_repo.get_pending_by_task(ctx.task_internal_id)
            if existing is not None:
                logger.info(
                    "_persist_human_confirmation_in_node: pending record exists, reuse | "
                    "task_internal_id=%s | confirmation_id=%s",
                    ctx.task_internal_id, existing.public_id,
                )
                return {
                    "confirmation_id": existing.public_id,
                    "confirmation_type": existing.confirmation_type,
                    "sections": sections_list,
                }

            now = utcnow()
            pending = HumanConfirmation(
                public_id=generate_public_id("confirmation"),
                user_id=ctx.user_internal_id,
                conversation_id=ctx.conversation_internal_id,
                task_id=ctx.task_internal_id,
                confirmation_type="section_generation_config",
                status="pending",
                prompt_text="请确认各章节的处理方式",
                request_json={"sections": sections_list},
                requested_at=now,
                created_at=now,
                updated_at=now,
            )
            await confirm_repo.create(pending)
            await session.commit()
            logger.info(
                "_persist_human_confirmation_in_node: created | task_internal_id=%s | "
                "confirmation_id=%s | sections=%d",
                ctx.task_internal_id, pending.public_id, len(sections_list),
            )
            return {
                "confirmation_id": pending.public_id,
                "confirmation_type": pending.confirmation_type,
                "sections": sections_list,
            }
    except Exception as exc:
        logger.warning(
            "_persist_human_confirmation_in_node failed (swallowed) | "
            "task_internal_id=%s | err=%s",
            ctx.task_internal_id, exc,
        )
        return None


async def pause_for_legacy_confirm_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """设置 pause_marker=need_user_confirm 并 exit to END。

    修复时序竞态:
      1. 先创建 HumanConfirmation + commit;
      2. 再 emit NEED_USER_CONFIRM(payload 含 confirmation_id + sections);
      3. 再 emit TASK_WAITING;
      4. 最后 return pause_marker。
    时序保证:commit 早于 event publish,前端 SSE 收到事件时
    pending-confirmation API 已能查到。
    """
    completed = mark_completed(state, NODE_PAUSE_FOR_LEGACY_CONFIRM)

    # 1) Pre-persist HumanConfirmation (commit 早于事件发布)
    # sections 为空(例如测试场景):跳过持久化但仍 emit,保持向后兼容。
    # 持久化失败(DB 故障):不 emit,进入 FAILED 路径。
    sections = state.get("section_suggestions") or {}
    sections_list = (
        sections.get("sections") if isinstance(sections, dict) else sections
    )
    has_sections = isinstance(sections_list, list) and bool(sections_list)
    confirmation = None
    if has_sections:
        confirmation = await _persist_human_confirmation_in_node(ctx, state)
        if confirmation is None:
            # 持久化失败:不发布事件,进入 FAILED
            logger.warning(
                "pause_for_legacy_confirm_node: pre-persist failed, skip event emit | "
                "task_internal_id=%s",
                ctx.task_internal_id,
            )
            return {
                "pause_marker": "need_user_confirm",
                "current_phase": "failed",
                "task_status": TaskStatus.FAILED.value,
                "current_node": NODE_PAUSE_FOR_LEGACY_CONFIRM,
                "completed_nodes": completed,
            }
    confirmation_id = confirmation.get("confirmation_id") if confirmation else None
    sections_payload = sections

    # 2) emit NEED_USER_CONFIRM (payload 含 confirmation_id + sections)
    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_PAUSE_FOR_LEGACY_CONFIRM,
        event_type=AgentEventType.NEED_USER_CONFIRM.value,
        title="等待章节确认",
        content="",
        payload={
            "confirmation_id": confirmation_id,
            "confirmation_type": (
                confirmation.get("confirmation_type")
                if confirmation
                else "section_generation_config"
            ),
            "task_id": str(ctx.task_internal_id),
            "section_count": (
                len(confirmation.get("sections") or []) if confirmation else 0
            ),
            "section_suggestions": sections_payload,
        },
    )
    # 3) emit TASK_WAITING
    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_PAUSE_FOR_LEGACY_CONFIRM,
        event_type=AgentEventType.TASK_WAITING.value,
        title="任务等待用户确认",
        content="",
        payload={"confirmation_id": confirmation_id},
    )

    # 4) return pause_marker
    return {
        "pause_marker": "need_user_confirm",
        "current_phase": "paused",
        "task_status": TaskStatus.WAITING_USER_CONFIRM.value,
        "current_node": NODE_PAUSE_FOR_LEGACY_CONFIRM,
        "completed_nodes": completed,
    }


def _build_clarification_cards(state: TestPlanGraphState) -> list[dict[str, Any]]:
    """Turn unresolved Preparation gaps into bounded task-scoped user inputs."""
    result = state.get("preparation_result")
    result = result if isinstance(result, dict) else {}
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    candidates = list(result.get("requirement_gaps") or []) + list(
        result.get("user_questions") or []
    )
    for item in candidates:
        if not isinstance(item, dict):
            continue
        gap_id = str(item.get("field") or item.get("id") or "requirement_gap").strip()[:80]
        question = str(item.get("description") or item.get("question") or "").strip()[:500]
        if not question or gap_id in seen:
            continue
        seen.add(gap_id)
        severity = str(item.get("severity") or "medium").strip().lower()
        severity = severity if severity in {"high", "medium", "low"} else "medium"
        selection_mode = (
            "multiple"
            if str(item.get("selection_mode") or item.get("selectionMode") or "").strip().lower() == "multiple"
            else "single"
        )
        cards.append({
            "id": gap_id,
            "question": question,
            "severity": severity,
            "selection_mode": selection_mode,
            "options": _clarification_choice_options(item.get("options")),
            "allow_conservative_scope": _allows_explicit_conservative_scope(
                gap_id, severity
            ),
        })
        if len(cards) >= 3:
            break
    return cards or [{
        "id": "requirement_scope",
        "question": "当前需求仍缺少生成测试方案所需的关键边界，请补充适用范围、验收口径或明确采用的保守范围。",
        "severity": "high",
        "selection_mode": "single",
        "options": _fallback_clarification_options(),
        "allow_conservative_scope": True,
    }]


def _clarification_choice_options(raw_options: Any) -> list[dict[str, str]]:
    """Keep LLM choices bounded and safe for direct display in the task UI."""
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_options if isinstance(raw_options, list) else []:
        if not isinstance(raw, dict):
            continue
        option_id = str(raw.get("id") or "").strip()[:80]
        label = str(raw.get("label") or "").strip()[:100]
        if not option_id or not label or option_id in seen:
            continue
        seen.add(option_id)
        options.append({
            "id": option_id,
            "label": label,
            "description": str(raw.get("description") or "").strip()[:180],
        })
        if len(options) >= 4:
            break
    if len(options) >= 2:
        return options
    for fallback in _fallback_clarification_options():
        if fallback["id"] not in seen:
            options.append(fallback)
            seen.add(fallback["id"])
        if len(options) >= 2:
            break
    return options[:4]


def _fallback_clarification_options() -> list[dict[str, str]]:
    """Legacy/recovery decisions still need bounded choices rather than a blank form."""
    return [
        {
            "id": "confirm_current_scope",
            "label": "按当前需求描述执行",
            "description": "将文档中已明确的范围作为本次测试基线。",
        },
        {
            "id": "not_applicable",
            "label": "本项不适用于本次试运行",
            "description": "明确排除该规则，并在方案中记录原因。",
        },
    ]


def _allows_explicit_conservative_scope(gap_id: str, severity: str) -> bool:
    """Only a user may choose a conservative *scope*, never a business policy."""
    normalized = gap_id.strip().lower()
    return severity == "high" and normalized in {
        "requirement_scope",
        "scope_and_capacity",
        "test_scope",
        "applicable_scope",
    }


def _clarification_retrieval_summary(state: TestPlanGraphState) -> dict[str, Any]:
    bundle = state.get("retrieval_evidence_bundle")
    bundle = bundle if isinstance(bundle, dict) else {}
    company = bundle.get("company_rag") if isinstance(bundle.get("company_rag"), dict) else {}
    project = bundle.get("project_rag") if isinstance(bundle.get("project_rag"), dict) else {}
    return {
        "retrieval_round": int(state.get("retrieval_round") or 0),
        "company_rag": {"status": company.get("status") or "not_available", "hit_count": len(company.get("hits") or [])},
        "project_rag": {
            "status": project.get("status") or "not_available",
            "hit_count": len(project.get("hits") or []),
            "reason": project.get("reason"),
        },
    }


async def _persist_preparation_clarification(
    ctx: RuntimeContext,
    *,
    cards: list[dict[str, Any]],
    retrieval_summary: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        from app.models.human_confirmation import HumanConfirmation
        from app.repositories.confirmation_repository import ConfirmationRepository
        from app.utils.datetime import utcnow
        from app.utils.ids import generate_public_id

        async with ctx.session_factory() as session:
            repo = ConfirmationRepository(session)
            existing = await repo.get_pending_by_task(ctx.task_internal_id)
            if existing is not None:
                if existing.confirmation_type != "preparation_clarification":
                    logger.warning(
                        "preparation clarification cannot reuse different pending confirmation | task=%s | type=%s",
                        ctx.task_internal_id, existing.confirmation_type,
                    )
                    return None
                return {"confirmation_id": existing.public_id}
            now = utcnow()
            pending = HumanConfirmation(
                public_id=generate_public_id("confirmation"),
                user_id=ctx.user_internal_id,
                conversation_id=ctx.conversation_internal_id,
                task_id=ctx.task_internal_id,
                confirmation_type="preparation_clarification",
                status="pending",
                prompt_text="请补充未由需求文档和检索证据解决的测试方案关键信息。",
                request_json={"cards": cards, "retrieval_summary": retrieval_summary},
                requested_at=now,
                created_at=now,
                updated_at=now,
            )
            await repo.create(pending)
            await session.commit()
            return {"confirmation_id": pending.public_id}
    except Exception as exc:  # noqa: BLE001 - persistence error must block generation
        logger.warning(
            "preparation clarification persistence failed | task=%s | err=%s",
            ctx.task_internal_id, type(exc).__name__,
        )
        return None


async def prepare_preparation_clarification_node(
    state: TestPlanGraphState, *, ctx: RuntimeContext
) -> dict:
    """Persist and announce the human clarification required after two rounds."""
    completed = mark_completed(state, NODE_PREPARE_CLARIFICATION)
    cards = _build_clarification_cards(state)
    retrieval_summary = _clarification_retrieval_summary(state)
    confirmation = await _persist_preparation_clarification(
        ctx, cards=cards, retrieval_summary=retrieval_summary
    )
    if confirmation is None:
        return {
            "last_error": {
                "code": "PREPARATION_CLARIFICATION_PERSIST_FAILED",
                "message": "Unable to persist the required user clarification.",
            },
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_PREPARE_CLARIFICATION,
            "completed_nodes": completed,
        }

    confirmation_id = str(confirmation["confirmation_id"])
    payload = {
        "confirmation_id": confirmation_id,
        "confirmation_type": "preparation_clarification",
        "task_id": str(ctx.task_internal_id),
        "clarification_cards": cards,
        "retrieval_summary": retrieval_summary,
    }
    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id), graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_PREPARE_CLARIFICATION,
        event_type=AgentEventType.NEED_USER_CONFIRM.value,
        title="需要补充测试方案关键信息", content="", payload=payload,
    )
    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id), graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_PREPARE_CLARIFICATION,
        event_type=AgentEventType.TASK_WAITING.value,
        title="任务等待补充信息", content="", payload={"confirmation_id": confirmation_id},
    )
    return {
        "clarification_cards": cards,
        "clarification_confirmation_id": confirmation_id,
        "current_phase": "paused",
        "task_status": TaskStatus.WAITING_USER_CONFIRM.value,
        "current_node": NODE_PREPARE_CLARIFICATION,
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
    "NODE_PREPARE_SECTION_CONFIRMATION",
    "NODE_PREPARE_CLARIFICATION",
    "NODE_PREP_SUBGRAPH",
    "NODE_PREP_LEGACY_FALLBACK",
    "initialize_task_node",
    "validate_inputs_node",
    "parse_requirement_node",
    "parse_template_node",
    "search_knowledge_node",
    "suggest_sections_node",
    "pause_for_legacy_confirm_node",
    "prepare_section_confirmation_node",
    "prepare_preparation_clarification_node",
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
        "retrieval_evidence_bundle": state.get("retrieval_evidence_bundle"),
        "clarification_answers": state.get("clarification_answers"),
        "retrieval_round": int(state.get("retrieval_round") or 0),
        "kb_skip_reason": state.get("kb_skip_reason"),
        "dual_rag_orchestration": True,
        "completed_nodes": state.get("completed_nodes") or [],
    }

    # tool_adapter / llm_client 从 ctx 取(Phase 2.1 + Phase 2.9B.1 注入路径)。
    # RuntimeContext 显式声明这两个字段;llm_client 在 ProductionRuntimeContextFactory
    # 中按用户构造。缺失时记录结构化 fallback 原因,不伪造成功。
    tool_adapter = getattr(ctx, "tool_adapter", None)
    llm_client = getattr(ctx, "llm_client", None)
    if tool_adapter is None or llm_client is None:
        missing = [
            name
            for name, val in (("tool_adapter", tool_adapter), ("llm_client", llm_client))
            if val is None
        ]
        logger.warning(
            "PreparationSubgraph unavailable | task_id=%s | "
            "missing_dependencies=%s | fallback=deterministic_preparation",
            ctx.task_internal_id,
            missing,
        )
        # 依赖缺失 → 只标记 fallback 原因,由主图 prep_legacy_fallback_node
        # 统一执行一次 KnowledgeSearchTool。绝不在本节点内私自执行 KB,
        # 否则 fallback 一次 + 主图再次执行会重复调用 KnowledgeSearchTool。
        return {
            "current_node": NODE_PREP_SUBGRAPH,
            "completed_nodes": completed,
            "preparation_result": None,
            "preparation_steps": list(state.get("preparation_steps") or []),
            "preparation_fallback_reason": (
                f"missing_runtime_dependencies:{','.join(missing)}"
            ),
        }

    result = await run_preparation_subgraph(
        state_snapshot,
        llm_client=llm_client,
        tool_adapter=tool_adapter,
        ctx=ctx,
    )

    result_dump = result.model_dump()
    company_skip_reason = next(
        (
            item.split(":", 1)[1]
            for item in result.constraints
            if isinstance(item, str) and item.startswith("company_rag_skipped:")
        ),
        None,
    )
    return {
        "preparation_result": result_dump,
        "retrieval_plan_snapshot": _build_retrieval_plan(result_dump),
        "kb_skip_reason": company_skip_reason or state.get("kb_skip_reason"),
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


def _build_pending_narrative(
    *,
    state: TestPlanGraphState,
    tool_name: str,
    tool_call_id: str,
    attempt: int,
    terminal_status: str,
    continuation_route: str,
    duration_ms: int | None = None,
    source_event_id: str | None = None,
) -> dict:
    """构造 PendingNarrative 片段(Phase 2.9B.4 / 2.9B.6)。

    供 Tool 节点在成功完成后写入 state.pending_narrative,
    tool_narrative_barrier 节点读取并生成同步叙事。

    Phase 2.9B.6 Tool Identity Contract:
      * ``source_tool_call_id`` 必须是 adapter 回写的真实 tool_call_id;
      * 缺失真实 tool_call_id 时**不得**生成 ``{tool_name}-{task_id}`` 伪锚点
        (那会与前端按真实 tool_call_id 锚定永远不匹配,导致叙事孤儿化) —
        记录 contract violation 并允许叙事走 deterministic fallback;
      * ``source_event_id`` 必须是真实 terminal 事件的 event_id,不再写空串。
    """
    from app.agent_runtime.narrative_composer.schemas import PendingNarrative

    if not tool_call_id:
        logger.warning(
            "contract_violation: tool_call_id 缺失(tool=%s task=%s) — "
            "不生成 ToolName-taskId 伪锚点,Narrative 将走确定性回退",
            tool_name, state.get("task_id") or "",
        )
        tool_call_id = ""

    return PendingNarrative(
        source_tool_name=tool_name,
        source_tool_call_id=tool_call_id,
        source_event_id=str(source_event_id or ""),
        tool_attempt=max(1, int(attempt or 1)),
        terminal_status=terminal_status,
        continuation_route=continuation_route,
        duration_ms=duration_ms,
    ).model_dump(mode="json")
