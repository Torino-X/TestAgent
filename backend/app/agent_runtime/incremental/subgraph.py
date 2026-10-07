"""Incremental Agent Subgraph (Phase 2.5).

镜像 ``preparation.subgraph`` / ``repair.subgraph`` 的设计:5-6 节点 LangGraph,
compile 返回 ``CompiledStateGraph``。

节点拓扑:

```
incremental_decide ─┬─→  incremental_execute_tool  ─→  incremental_observe  ─┐
                    │                                                       │
                    └────────────────── re-iterate ────────────────────────┘

                    incremental_decide ─finish/ask_user─→  incremental_finish  ─→ END
                    incremental_observe ─permanent_deny/budget/fail─→  incremental_fallback  ─→ END
```

`graph_name="incremental_test_plan"` 与 Phase 2.1 / 2.4
`"test_plan_generation"` 隔离 —— 老任务不会被新 subgraph 误识别。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, List, Tuple

from langgraph.graph import END, START, StateGraph

from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState


_logger = logging.getLogger(__name__)


def _utcnow():
    from app.utils.datetime import utcnow

    return utcnow()


# ── Node names (常量) ─────────────────────────────────────────────


NODE_INCREMENTAL_DECIDE = "incremental_decide"
NODE_INCREMENTAL_EXECUTE_TOOL = "incremental_execute_tool"
NODE_INCREMENTAL_OBSERVE = "incremental_observe"
NODE_INCREMENTAL_FINISH = "incremental_finish"
NODE_INCREMENTAL_FALLBACK = "incremental_fallback"


# ── Node functions(轻量代理 —— 真实逻辑在 agent_loop) ──────────────


async def incremental_decide_node(
    state: TestPlanGraphState, *, ctx=None,
) -> Dict[str, Any]:
    """入口节点:触发 ``run_incremental`` 一次 step。"""
    from app.agent_runtime.incremental.agent_loop import run_incremental

    state_dict = dict(state)
    result = await run_incremental(state_dict, ctx=ctx)

    return {
        "incremental_result": result.model_dump(mode="json"),
        "incremental_steps": state_dict.get("incremental_steps", []),
        "current_node": NODE_INCREMENTAL_DECIDE,
        "completed_nodes": (state.get("completed_nodes") or [])
        + [NODE_INCREMENTAL_DECIDE],
    }


async def incremental_execute_tool_node(
    state: TestPlanGraphState, *, ctx=None,
) -> Dict[str, Any]:
    """stub —— 实际工具执行由 ``incremental_decide_node`` 驱动;
    此节点保留拓扑完整性,允许 Phase 2.6+ 拆出单独 tool 节点。"""
    return {
        "current_node": NODE_INCREMENTAL_EXECUTE_TOOL,
        "completed_nodes": (state.get("completed_nodes") or [])
        + [NODE_INCREMENTAL_EXECUTE_TOOL],
    }


async def incremental_observe_node(
    state: TestPlanGraphState, *, ctx=None,
) -> Dict[str, Any]:
    """观察节点 —— 检查 ``incremental_result.success``,决定路由。"""
    result = state.get("incremental_result") or {}
    success = bool(result.get("success"))
    fallback = bool(result.get("fallback_reason"))

    return {
        "incremental_observed_success": success,
        "incremental_observed_fallback": fallback,
        "current_node": NODE_INCREMENTAL_OBSERVE,
        "completed_nodes": (state.get("completed_nodes") or [])
        + [NODE_INCREMENTAL_OBSERVE],
    }


async def incremental_finish_node(
    state: TestPlanGraphState, *, ctx=None,
) -> Dict[str, Any]:
    return {
        "task_status": "completed" if (state.get("incremental_result") or {}).get("success") else "failed",
        "current_phase": "incremental_completed",
        "current_node": NODE_INCREMENTAL_FINISH,
        "completed_nodes": (state.get("completed_nodes") or [])
        + [NODE_INCREMENTAL_FINISH],
    }


async def incremental_fallback_node(
    state: TestPlanGraphState, *, ctx=None,
) -> Dict[str, Any]:
    """fallback 节点 —— 已由 ``run_incremental`` 内部触发 fallback,
    此处只是 emit 终止事件 + 写 final status。"""
    result = state.get("incremental_result") or {}
    return {
        "task_status": "completed" if result.get("success") else "failed",
        "current_phase": "incremental_fallback_completed",
        "current_node": NODE_INCREMENTAL_FALLBACK,
        "completed_nodes": (state.get("completed_nodes") or [])
        + [NODE_INCREMENTAL_FALLBACK],
    }


# ── Routers ──────────────────────────────────────────────────────


def route_after_decide(state: TestPlanGraphState) -> str:
    """Incremental 主循环内的路由器。"""
    result = state.get("incremental_result") or {}
    if result.get("fallback_reason"):
        return NODE_INCREMENTAL_FALLBACK
    if result.get("success"):
        return NODE_INCREMENTAL_FINISH
    if result.get("ask_user_pending"):
        return NODE_INCREMENTAL_FINISH  # 也走 finish 节点,UI 走 ask_user 通道
    return NODE_INCREMENTAL_FALLBACK


def route_after_observe(state: TestPlanGraphState) -> str:
    """观察节点后路由 —— 通常直接到 finish / fallback。"""
    return (
        NODE_INCREMENTAL_FINISH
        if state.get("incremental_observed_success")
        else NODE_INCREMENTAL_FALLBACK
    )


# ── Build subgraph ───────────────────────────────────────────────


GRAPH_NAME_INCREMENTAL = "incremental_test_plan"
GRAPH_VERSION_INCREMENTAL_V1 = "v1"
GRAPH_VERSION_INCREMENTAL_V3 = "v3"


def build_incremental_subgraph(checkpointer=None):
    """编译并返回 incremental_test_plan v1 LangGraph。

    Args:
        checkpointer: 兼容 LangGraph checkpointer(Phase 2.5 默认 MemorySaver)

    Returns:
        compiled ``StateGraph``
    """
    g = StateGraph(TestPlanGraphState)

    # 节点
    g.add_node(NODE_INCREMENTAL_DECIDE, incremental_decide_node)
    g.add_node(NODE_INCREMENTAL_EXECUTE_TOOL, incremental_execute_tool_node)
    g.add_node(NODE_INCREMENTAL_OBSERVE, incremental_observe_node)
    g.add_node(NODE_INCREMENTAL_FINISH, incremental_finish_node)
    g.add_node(NODE_INCREMENTAL_FALLBACK, incremental_fallback_node)

    # 入口
    g.add_edge(START, NODE_INCREMENTAL_DECIDE)
    g.add_edge(NODE_INCREMENTAL_DECIDE, NODE_INCREMENTAL_OBSERVE)
    g.add_conditional_edges(
        NODE_INCREMENTAL_OBSERVE,
        route_after_observe,
        {
            NODE_INCREMENTAL_FINISH: NODE_INCREMENTAL_FINISH,
            NODE_INCREMENTAL_FALLBACK: NODE_INCREMENTAL_FALLBACK,
        },
    )
    g.add_edge(NODE_INCREMENTAL_FINISH, END)
    g.add_edge(NODE_INCREMENTAL_FALLBACK, END)

    return g.compile(
        checkpointer=checkpointer,
        name=f"{GRAPH_NAME_INCREMENTAL}_{GRAPH_VERSION_INCREMENTAL_V1}",
    )


# ── Public runner ────────────────────────────────────────────────


async def run_incremental_subgraph(
    state: Dict[str, Any],
    *,
    ctx,
    config: Dict[str, Any] | None = None,
    checkpointer=None,
) -> Dict[str, Any]:
    """运行增量子 Agent,返回 final state。

    增量 Agent 与 preparation / repair 子图一样依赖 RuntimeContext
    (event_sink / llm_client / tool_adapter / session_factory)。LangGraph
    ``ainvoke`` 只接收 ``state`` 和 ``config``，不会把自定义 ``ctx`` kwarg
    自动注入到节点函数。生产入口必须显式把 ctx 传入 ``run_incremental``，
    否则节点内会拿到 ``None``，导致事件发送、LLM 调用和 fallback 全部失败。

    Phase 2.8R-C:task_id 缺失/空/stub-thread 一律抛 ``InvalidGraphThreadIdError``。
    """
    # 严格 thread_id:不静默回退到 ``incremental-default``
    from app.agent_runtime.graph_thread_id import require_graph_thread_id
    from app.agent_runtime.incremental.agent_loop import run_incremental

    thread_id = require_graph_thread_id(state, context="incremental_subgraph")
    cfg = config or {"configurable": {"thread_id": thread_id}}
    runtime_ctx = ctx
    if runtime_ctx is None:
        runtime_ctx = (cfg.get("configurable") or {}).get("runtime_context")
    if runtime_ctx is None:
        raise RuntimeError(
            "incremental_runtime_context_missing: "
            "IncrementalAgent requires RuntimeContext, but ctx was not provided"
        )

    state_dict = dict(state)
    started_at = state_dict.get("incremental_started_at") or _utcnow().isoformat()
    result = await run_incremental(state_dict, ctx=runtime_ctx)
    result_payload = result.model_dump(mode="json")
    success = bool(result.success)
    fallback = bool(result.fallback_reason)
    finish_node = (
        NODE_INCREMENTAL_FALLBACK
        if fallback and not success
        else NODE_INCREMENTAL_FINISH
    )

    task_id_str = state_dict.get("task_id") or state.get("task_public_id") or ""
    graph_run_id = state_dict.get("graph_run_id") or ""
    from app.agent_runtime.incremental.event_emitter import IncrementalEventEmitter
    from app.agent.enums import AgentEventType
    terminal_emitter = IncrementalEventEmitter(
        runtime_ctx, node_name=finish_node,
    )

    # R6 — incremental 成功后必须**导出新 docx** 并 emit task_completed,
    # 否则前端 SSE /events 端点不识别终态、`state.artifact` 永远空,
    # 用户拿不到下载链接,WordExportTool + DocxFormatCheckTool 也从未跑过。
    new_artifact: dict | None = None
    format_check: dict = {}
    if success and (state_dict.get("test_plan_content") or {}):
        new_artifact, format_check = await _export_and_check_after_incremental(
            state_dict=state_dict,
            ctx=runtime_ctx,
            terminal_emitter=terminal_emitter,
            task_id_str=task_id_str,
            graph_run_id=graph_run_id,
        )
        pending_losses = list(format_check.get("pending_format_losses") or [])
        if pending_losses:
            state_dict["pending_format_losses"] = pending_losses
            state_dict["format_check_result"] = format_check
            state_dict["task_status"] = "format_loss_review"
            state_dict["current_phase"] = "paused"
            state_dict["current_node"] = NODE_INCREMENTAL_FINISH
            state_dict["pause_marker"] = "format_loss_review"
            await _emit_incremental_format_loss_confirmation(
                state_dict=state_dict,
                ctx=runtime_ctx,
                task_id_str=task_id_str,
                graph_run_id=graph_run_id,
                losses=pending_losses,
            )
            return {
                **state_dict,
                "incremental_result": result_payload,
                "incremental_steps": state_dict.get("incremental_steps", []),
                "incremental_observed_success": success,
                "incremental_observed_fallback": fallback,
                "task_status": "format_loss_review",
                "current_phase": "paused",
                "completed_nodes": _append_completed_nodes(
                    list(state_dict.get("completed_nodes") or state.get("completed_nodes") or []),
                    [NODE_INCREMENTAL_DECIDE, NODE_INCREMENTAL_OBSERVE, NODE_INCREMENTAL_FINISH],
                ),
            }
        if new_artifact and format_check.get("status") != "failed":
            state_dict["artifact"] = new_artifact
            state_dict["format_check_result"] = format_check
            state_dict["checked_artifact_public_id"] = (
                new_artifact.get("public_id") if isinstance(new_artifact, dict) else None
            )

    if success and (state_dict.get("test_plan_content") or {}) and (
        not new_artifact or format_check.get("status") == "failed"
    ):
        success = False

    if success:
        summary_facts = _incremental_summary_facts(
            state_dict=state_dict,
            new_artifact=new_artifact,
            format_check=format_check,
            modified_section_ids=result.modified_section_ids or [],
        )
        summary_result = await _generate_incremental_task_summary(
            state_dict=state_dict,
            ctx=runtime_ctx,
        )
        summary = _incremental_summary_text(state_dict, summary_result)
        completed_at = _utcnow()
        terminal_metadata = {
            "summary": summary,
            "summary_facts": summary_facts,
            "artifact": new_artifact,
            "format_check": format_check,
            "checked_artifact_public_id": state_dict.get("checked_artifact_public_id"),
            "started_at": started_at,
            "completed_at": completed_at.isoformat(),
            "duration_ms": _duration_ms(started_at, completed_at.isoformat()),
        }
        # This ordinary terminal event is what the existing completion-card
        # renderer consumes. Emit it first because the live client closes its
        # SSE connection as soon as it sees the authoritative terminal event.
        await runtime_ctx.event_sink.emit(
            task_id=str(getattr(runtime_ctx, "task_internal_id", task_id_str)),
            graph_run_id=f"run-{getattr(runtime_ctx, 'task_internal_id', task_id_str)}",
            node_name=NODE_INCREMENTAL_FINISH,
            event_type=AgentEventType.TASK_COMPLETED.value,
            title="任务已完成",
            content=summary,
            payload=terminal_metadata,
        )
        # Keep the incremental terminal marker additive for audit/replay.
        await terminal_emitter.emit_completed(
            task_id=task_id_str,
            graph_run_id=graph_run_id,
            result=result,
            extra=terminal_metadata,
        )
    else:
        failure_reason = (
            result.fallback_reason
            or (result.public_summary.detail if result.public_summary else None)
            or "incremental_failed"
        )
        await terminal_emitter.emit_failed(
            task_id=task_id_str,
            graph_run_id=graph_run_id,
            reason=failure_reason,
            extra={
                "fallback_reason": result.fallback_reason,
                "modified_section_ids": result.modified_section_ids,
                "public_summary": (
                    result.public_summary.model_dump(mode="json")
                    if result.public_summary else None
                ),
            },
        )

    completed_nodes = _append_completed_nodes(
        list(state_dict.get("completed_nodes") or state.get("completed_nodes") or []),
        [
            NODE_INCREMENTAL_DECIDE,
            NODE_INCREMENTAL_OBSERVE,
            finish_node,
        ],
    )

    return {
        **state_dict,
        "incremental_result": result_payload,
        "incremental_steps": state_dict.get("incremental_steps", []),
        "incremental_observed_success": success,
        "incremental_observed_fallback": fallback,
        "task_status": "completed" if success else "failed",
        "current_phase": (
            "incremental_completed"
            if success
            else "incremental_fallback_completed"
        ),
        "current_node": finish_node,
        "completed_nodes": completed_nodes,
    }


def _append_completed_nodes(existing: List[str], nodes: List[str]) -> List[str]:
    out = list(existing)
    for node in nodes:
        if not out or out[-1] != node:
            out.append(node)
    return out


def _incremental_summary_text(state_dict: Dict[str, Any], narrative_result=None) -> str:
    """R7 — 复用 v3 ``fallback_summary_text`` 作为增量任务终态文案。

    v3 finalize_task_node 用同一个函数渲染普通任务终态,前端模板渲染
    同一份 schema(content_length / chapter_count / review_block_count
    等)。增量任务直接用 → 卡片 UI 完全一致。
    """
    if narrative_result is not None and getattr(narrative_result, "success", False) and getattr(narrative_result, "source", "") == "llm":
        public_update = getattr(narrative_result, "public_update", None)
        narrative_text = getattr(public_update, "narrative_text", None)
        if isinstance(narrative_text, str) and narrative_text.strip():
            return narrative_text.strip()
        narrative_summary = getattr(public_update, "summary", None)
        if isinstance(narrative_summary, str) and narrative_summary.strip():
            return narrative_summary.strip()
    return "本次基于既有测试方案完成增量修改，并已导出新的测试方案产物。"


def _duration_ms(started_at: str, completed_at: str) -> int:
    from datetime import datetime

    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        completed = datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
        return max(0, round((completed - started).total_seconds() * 1000))
    except (TypeError, ValueError):
        return 0


async def _generate_incremental_task_summary(*, state_dict: Dict[str, Any], ctx):
    """Generate a user-facing summary without exposing model reasoning."""
    from app.agent_runtime.feature_flags import get_feature_flags
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_narrative import (
        _DETERMINISTIC_TASK_SUMMARY_FALLBACK,
        _build_task_summary_context,
        _resolve_narrative_llm,
    )
    from app.agent_runtime.narrative_composer.composer import NarrativeComposer
    from app.agent_runtime.narrative_composer.prompts import (
        build_incremental_task_summary_prompt,
    )
    from app.agent_runtime.narrative_composer.schemas import NarrativeGenerationRequest
    from app.llm.task_profiles import TASK_SUMMARY_NARRATIVE_COMPOSER_PROFILE
    import uuid

    flags = get_feature_flags()
    if not flags.phase29b_task_summary_narrative_enabled:
        return None
    llm_client = _resolve_narrative_llm(ctx, call_site="incremental.task_summary_narrative")
    if llm_client is None:
        return None

    context = _build_task_summary_context(dict(state_dict))
    context = context.model_copy(
        update={
            "task_goal": "基于已生成的测试方案，对指定章节进行增量修改并重新导出新版本",
        }
    )
    system_prompt, user_content = build_incremental_task_summary_prompt(context)
    narrative_id = f"inc_tsum_{uuid.uuid4().hex[:12]}"
    generation_id = f"inc_tsumgen_{uuid.uuid4().hex[:12]}"
    request = NarrativeGenerationRequest(
        kind="task_summary",
        narrative_id=narrative_id,
        generation_id=generation_id,
        generation_no=1,
        task_summary_context=context,
        system_prompt=system_prompt,
        user_content=user_content,
    )
    composer = NarrativeComposer(
        llm_client,
        ctx.event_sink,
        deterministic_fallback=_DETERMINISTIC_TASK_SUMMARY_FALLBACK,
        timeout_seconds=flags.phase29b_narrative_timeout_seconds,
        repair_attempts=flags.phase29b_narrative_repair_attempts,
    )
    try:
        return await composer._run_generation(
            task_internal_id=ctx.task_internal_id,
            graph_run_id=str(state_dict.get("graph_run_id") or f"run-{ctx.task_internal_id}"),
            request=request,
            context_for_fallback=context,
            event_prefix="task_summary",
            # The Context Engine bridge uses PLAIN_TEXT + a 24-character cap
            # when no profile is supplied.  A task-summary narrative must keep
            # its <NARRATIVE> wrapper and Markdown intact for
            # NarrativeStreamDecoder, exactly like the main test-plan summary
            # node does.
            profile=TASK_SUMMARY_NARRATIVE_COMPOSER_PROFILE,
        )
    except Exception:  # noqa: BLE001
        _logger.warning("incremental task-summary narrative failed", exc_info=True)
        return None


def _incremental_summary_facts(
    *,
    state_dict: Dict[str, Any],
    new_artifact: Dict[str, Any] | None,
    format_check: Dict[str, Any],
    modified_section_ids: List[str],
) -> Dict[str, Any]:
    """R7 — 复用 v3 ``build_summary_facts`` 算标准事实,叠加增量特有字段。

    新增的 incremental 字段:
      * ``incremental.modified_section_ids`` — 本次改的章节
      * ``incremental.previous_artifact_public_id`` — source artifact
      * ``incremental.new_artifact_public_id`` — 新 artifact
    """
    try:
        from app.agent_runtime._shared.summary_facts import build_summary_facts
        facts = build_summary_facts(dict(state_dict) if state_dict else {})
    except Exception:  # noqa: BLE001
        facts = {}

    facts["incremental"] = {
        "modified_section_ids": list(modified_section_ids or []),
        "previous_artifact_public_id": state_dict.get("source_artifact_public_id"),
        "new_artifact_public_id": (
            new_artifact.get("public_id") if isinstance(new_artifact, dict) else None
        ),
    }
    return facts


async def _export_and_check_after_incremental(
    *,
    state_dict: Dict[str, Any],
    ctx,
    terminal_emitter,
    task_id_str: str,
    graph_run_id: str,
) -> Tuple[Dict[str, Any] | None, Dict[str, Any]]:
    """R6 — incremental 成功后落地 docx。

    镜像 v3 ``export_word_node`` + ``check_docx_format_node``:
      1. 调 ``WordExportTool``(输入: test_plan_content + template_structure + template_file_id)
         → 拿到新 artifact dict
      2. emit ``artifact_created``
      3. 调 ``DocxFormatCheckTool``(输入: artifact + artifact_path)
         → 拿到 format_check_result
      4. emit ``docx_format_checked``

    任一步失败 → emit ``tool_failed`` 事件 + 返回 (None, {}),
    让 incremental_completed 事件仍然能 emit(虽然没新 artifact,内容已写到 state)。
    """
    from app.agent.enums import AgentEventType
    from app.agent_runtime._shared.artifact_contract import normalize_export_artifact

    if ctx is None or getattr(ctx, "tool_adapter", None) is None:
        return None, {}

    adapter = ctx.tool_adapter
    test_plan_content = state_dict.get("test_plan_content") or {}
    template_structure = state_dict.get("template_structure") or {}
    template_file_id = state_dict.get("template_file_id")

    # ── WordExportTool ──
    export_envelope = await adapter.execute(
        tool_name="WordExportTool",
        inputs={
            "test_plan_content": test_plan_content,
            "template_structure": template_structure,
            "template_file_id": template_file_id,
            "fidelity": "high",
        },
        ctx_runtime=ctx,
        graph_state=dict(state_dict) if state_dict else None,
    )
    if not export_envelope or not export_envelope.get("success"):
        await ctx.event_sink.emit(
            task_id=str(getattr(ctx, "task_internal_id", task_id_str)),
            graph_run_id=f"run-{getattr(ctx, 'task_internal_id', task_id_str)}",
            node_name=NODE_INCREMENTAL_FINISH,
            event_type=AgentEventType.TOOL_FAILED.value,
            title="增量任务导出 docx 失败",
            content=(export_envelope or {}).get("error", {}).get("message")
            if isinstance((export_envelope or {}).get("error"), dict)
            else "WordExportTool 未返回成功",
            payload={"tool": "WordExportTool"},
        )
        return None, {}

    data = (export_envelope.get("data") or {})
    export_pending_losses = list(data.get("pending_format_losses") or [])
    # WordExportTool returns the artifact fields directly in envelope.data
    # (not under data["artifact"]); the ordinary v3 flow uses this same
    # normalizer and aliases artifact_id to public_id for Graph State.
    raw_artifact = (
        data.get("artifact")
        if isinstance(data.get("artifact"), dict)
        else data
    )
    normalized = normalize_export_artifact(raw_artifact)
    if not normalized.ok:
        await ctx.event_sink.emit(
            task_id=str(getattr(ctx, "task_internal_id", task_id_str)),
            graph_run_id=f"run-{getattr(ctx, 'task_internal_id', task_id_str)}",
            node_name=NODE_INCREMENTAL_FINISH,
            event_type=AgentEventType.TOOL_FAILED.value,
            title="增量任务导出产物校验失败",
            content="WordExportTool 返回的产物不满足格式检查要求。",
            payload={
                "tool": "WordExportTool",
                "errors": list(normalized.errors),
                "warnings": list(normalized.warnings),
            },
        )
        return None, {}
    new_artifact = normalized.artifact

    await ctx.event_sink.emit(
        task_id=str(getattr(ctx, "task_internal_id", task_id_str)),
        graph_run_id=f"run-{getattr(ctx, 'task_internal_id', task_id_str)}",
        node_name=NODE_INCREMENTAL_FINISH,
        event_type=AgentEventType.ARTIFACT_CREATED.value,
        title="增量产物已生成",
        content="",
        payload={"artifact": new_artifact},
    )

    # ── DocxFormatCheckTool ──
    artifact_path = new_artifact.get("storage_path")
    check_envelope = await adapter.execute(
        tool_name="DocxFormatCheckTool",
        inputs={
            "artifact": new_artifact,
            "artifact_path": artifact_path,
            "format_loss_confirmation": state_dict.get("format_loss_confirmation") or {},
        },
        ctx_runtime=ctx,
        graph_state=dict(state_dict) if state_dict else None,
    )
    if not check_envelope or not check_envelope.get("success"):
        error = (check_envelope or {}).get("error") or {}
        await ctx.event_sink.emit(
            task_id=str(getattr(ctx, "task_internal_id", task_id_str)),
            graph_run_id=f"run-{getattr(ctx, 'task_internal_id', task_id_str)}",
            node_name=NODE_INCREMENTAL_FINISH,
            event_type=AgentEventType.TOOL_FAILED.value,
            title="澧為噺浠诲姟鏍煎紡妫€鏌ュけ璐?",
            content=error.get("message") if isinstance(error, dict) else "DocxFormatCheckTool 鏈繑鍥炴垚鍔?",
            payload={"tool": "DocxFormatCheckTool"},
        )
        return new_artifact, {"level": "failed", "status": "failed"}

    check_data = (check_envelope or {}).get("data") or {}
    raw_level = (check_data.get("level") or "passed") if isinstance(check_data, dict) else "passed"
    status_alias = "blocked" if raw_level == "loss_detected" else raw_level
    pending_losses = list(check_data.get("losses") or []) if raw_level == "loss_detected" else []
    pending_losses = pending_losses or export_pending_losses
    format_check = (
        {**(check_data if isinstance(check_data, dict) else {}),
         "level": raw_level, "status": status_alias,
         "pending_format_losses": pending_losses,
         "format_loss_pending": bool(pending_losses)}
    )

    await ctx.event_sink.emit(
        task_id=str(getattr(ctx, "task_internal_id", task_id_str)),
        graph_run_id=f"run-{getattr(ctx, 'task_internal_id', task_id_str)}",
        node_name=NODE_INCREMENTAL_FINISH,
        event_type=AgentEventType.DOCX_FORMAT_CHECKED.value,
        title="格式自检",
        content=f"level={raw_level}",
        payload={"format_check": format_check, "artifact": new_artifact},
    )

    return new_artifact, format_check


async def _emit_incremental_format_loss_confirmation(
    *, state_dict: Dict[str, Any], ctx, task_id_str: str, graph_run_id: str,
    losses: List[Any],
) -> None:
    from app.agent.enums import AgentEventType

    timeout_seconds = int(getattr(ctx, "format_loss_timeout_seconds", 300) or 300)
    timeout_at = (_utcnow() + timedelta(seconds=timeout_seconds)).isoformat()
    await ctx.event_sink.emit(
        task_id=str(getattr(ctx, "task_internal_id", task_id_str)),
        graph_run_id=f"run-{getattr(ctx, 'task_internal_id', task_id_str)}",
        node_name=NODE_INCREMENTAL_FINISH,
        event_type=AgentEventType.FORMAT_LOSS_CONFIRM_REQUESTED.value,
        title="发现文档格式丢失，等待确认",
        content=f"检测到 {len(losses)} 项格式差异，请选择是否继续使用当前导出结果。",
        payload={
            "losses": losses,
            "loss_count": len(losses),
            "loss_details_for_user": f"检测到 {len(losses)} 项格式差异",
            "choices": [
                {"id": "retry", "label": "重新生成文档"},
                {"id": "accept", "label": "继续使用当前文档"},
            ],
            "timeout_at": timeout_at,
            "incremental": True,
        },
    )


__all__ = [
    "GRAPH_NAME_INCREMENTAL",
    "GRAPH_VERSION_INCREMENTAL_V1",
    "GRAPH_VERSION_INCREMENTAL_V3",
    "NODE_INCREMENTAL_DECIDE",
    "NODE_INCREMENTAL_EXECUTE_TOOL",
    "NODE_INCREMENTAL_OBSERVE",
    "NODE_INCREMENTAL_FINISH",
    "NODE_INCREMENTAL_FALLBACK",
    "incremental_decide_node",
    "incremental_execute_tool_node",
    "incremental_observe_node",
    "incremental_finish_node",
    "incremental_fallback_node",
    "route_after_decide",
    "route_after_observe",
    "build_incremental_subgraph",
    "run_incremental_subgraph",
]

# module-level note (auto-appended):
# build_incremental_subgraph — graph_name=incremental_test_plan。
# 关键约束: checkpointer MemorySaver。
