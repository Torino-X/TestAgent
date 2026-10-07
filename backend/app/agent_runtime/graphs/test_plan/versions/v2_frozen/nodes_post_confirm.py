"""v2 post-confirm 节点 (Legacy orchestrator resume_after_confirm 段)。

节点:
* ``resume_task``                      — 进 post_confirm
* ``generate_test_plan``               — TestPlanGeneratorTool + retry
* ``prepare_export``                   — task_status=exporting
* ``export_word``                      — WordExportTool + retry,装入 artifact
* ``pause_for_legacy_format_decision`` — pause_marker=format_loss_review
* ``record_loss_decision``             — 把用户决策落库 → 跳 finalize_task
* ``generate_completion_summary_node`` — Phase 2.8D:复用 Legacy 摘要生成
* ``finalize_task``                    — TASK_COMPLETED
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Dict, Optional

from app.agent.enums import AgentEventType, TaskStatus

from app.agent_runtime.runtime_context import RuntimeContext
from app.integrations.llm_client import LLMClient

from app.agent_runtime.graphs.nodes_emit_helper import _emit_with_public_update
from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState
from .nodes_util import mark_completed

logger = logging.getLogger(__name__)


NODE_RESUME_TASK = "resume_task"
NODE_GENERATE_TEST_PLAN = "generate_test_plan"
NODE_PREPARE_EXPORT = "prepare_export"
NODE_EXPORT_WORD = "export_word"
NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION = "pause_for_legacy_format_decision"
NODE_RECORD_LOSS_DECISION = "record_loss_decision"
# Phase 2.8D: 复用 Legacy _generate_completion_summary 逻辑
NODE_GENERATE_COMPLETION_SUMMARY = "generate_completion_summary"
NODE_FINALIZE_TASK = "finalize_task"


def _adapter(ctx: RuntimeContext):
    return getattr(ctx, "tool_adapter", None)


def _core_failed(envelope: Dict[str, Any]) -> bool:
    if envelope.get("success"):
        return False
    err = envelope.get("error") or {}
    if not isinstance(err, dict):
        return False
    if err.get("recoverable") is False:
        return True
    if err.get("code") in {
        "WORD_EXPORT_CONTRACT_ERROR",
        "EXPORT_TEMPLATE_NOT_FOUND",
        "INTEGRITY_CHECK_FAILED",
        "EXPORT_TEMPLATE_INVALID",
    }:
        return True
    return False


# ── resume_task ──────────────────────────────────────────────────────────────


async def resume_task_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """进入 post_confirm 段。用户决策已经被 coordinator 写进 ``section_confirm_config``。"""
    completed = mark_completed(state, NODE_RESUME_TASK)
    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_RESUME_TASK,
        event_type=AgentEventType.TASK_RESUMED.value,
        title="任务已恢复",
        content="",
        payload={},
    )
    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_RESUME_TASK,
        event_type=AgentEventType.GENERATING_STARTED.value,
        title="开始生成测试方案",
        content="",
        payload={},
    )
    return {
        "pause_marker": None,
        "current_phase": "post_confirm",
        "task_status": TaskStatus.GENERATING.value,
        "current_node": NODE_RESUME_TASK,
        "completed_nodes": completed,
    }


# ── generate_test_plan ───────────────────────────────────────────────────────


async def generate_test_plan_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """调 TestPlanGeneratorTool(由 adapter 处理 retry 与 pacing)。"""
    completed = mark_completed(state, NODE_GENERATE_TEST_PLAN)
    adapter = _adapter(ctx)
    if adapter is None:
        return {
            "test_plan_content": {},
            "current_node": NODE_GENERATE_TEST_PLAN,
            "completed_nodes": completed,
        }

    envelope = await adapter.execute(
        tool_name="TestPlanGeneratorTool",
        inputs={
            "requirement_analysis": state.get("requirement_analysis") or {},
            "template_structure": state.get("template_structure") or {},
            "section_confirm_config": state.get("section_confirm_config") or {},
            "knowledge_search_result": state.get("knowledge_search_result") or {},
        },
        ctx_runtime=ctx,
    )

    if _core_failed(envelope):
        return {
            "last_error": envelope.get("error") or {"code": "GENERATION_FAILED"},
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_GENERATE_TEST_PLAN,
            "completed_nodes": completed,
        }

    return {
        "test_plan_content": (envelope.get("data") or {}),
        "review_loop_count": 0,
        "current_node": NODE_GENERATE_TEST_PLAN,
        "completed_nodes": completed,
    }


# ── prepare_export ───────────────────────────────────────────────────────────


async def prepare_export_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """切到 exporting status;WordExportTool 一会儿会读 state.artifact 续传。"""
    completed = mark_completed(state, NODE_PREPARE_EXPORT)
    return {
        "task_status": TaskStatus.EXPORTING.value,
        "current_node": NODE_PREPARE_EXPORT,
        "completed_nodes": completed,
    }


# ── export_word ──────────────────────────────────────────────────────────────


async def export_word_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """调 WordExportTool。fidelity_hint 来自 ``_next_fidelity(format_loop_count)`` 镜像。"""
    completed = mark_completed(state, NODE_EXPORT_WORD)
    adapter = _adapter(ctx)
    if adapter is None:
        return {
            "artifact": {"file_name": "stub.docx", "file_ext": "docx"},
            "current_node": NODE_EXPORT_WORD,
            "completed_nodes": completed,
        }

    loops = int(state.get("format_loop_count") or 0)
    fidelity = {1: "medium", 2: "low"}.get(loops + 1, "low")

    envelope = await adapter.execute(
        tool_name="WordExportTool",
        inputs={
            "test_plan_content": state.get("test_plan_content") or {},
            "template_structure": state.get("template_structure") or {},
            "fidelity_hint": fidelity,
        },
        ctx_runtime=ctx,
    )

    if _core_failed(envelope):
        return {
            "last_error": envelope.get("error") or {"code": "WORD_EXPORT_FAILED"},
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_EXPORT_WORD,
            "completed_nodes": completed,
        }

    artifact = envelope.get("data") or {}
    return {
        "artifact": artifact if isinstance(artifact, dict) else {"artifact": artifact},
        "last_retry_strategy": None,
        "current_node": NODE_EXPORT_WORD,
        "completed_nodes": completed,
    }


# ── pause_for_legacy_format_decision ────────────────────────────────────────


async def pause_for_legacy_format_decision_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """设置 pause_marker=format_loss_review,等用户在 API 端决策。"""
    completed = mark_completed(state, NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION)
    losses = state.get("pending_format_losses") or []

    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION,
        event_type=AgentEventType.FORMAT_LOSS_CONFIRM_REQUESTED.value,
        title="格式丢失需要确认",
        content="",
        payload={"losses": losses},
    )

    return {
        "pause_marker": "format_loss_review",
        "current_phase": "paused",
        "task_status": TaskStatus.FORMAT_LOSS_REVIEW.value,
        "pending_format_losses": losses,
        "current_node": NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION,
        "completed_nodes": completed,
    }


# ── record_loss_decision ─────────────────────────────────────────────────────


async def record_loss_decision_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """读 ``state.format_loss_confirmation``,记录决策;若 accept+retry 则进 prepare_export 重试,
    若 accept 直接接受则进 finalize_task,若 reject 则视作 fail_task。"""
    completed = mark_completed(state, NODE_RECORD_LOSS_DECISION)
    confirmation = state.get("format_loss_confirmation") or {}
    decision = str(confirmation.get("decision") or "accept")
    pending = state.get("pending_format_losses") or []

    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_RECORD_LOSS_DECISION,
        event_type=AgentEventType.FORMAT_LOSS_DECISION_RECORDED.value,
        title=f"用户决策:{decision}",
        content="",
        payload={"decision": decision, "losses": pending},
    )

    new_state: Dict[str, Any] = {
        "format_loss_confirmation": confirmation,
        "current_node": NODE_RECORD_LOSS_DECISION,
        "completed_nodes": completed,
    }

    if decision == "accept":
        new_state.update({
            "task_status": TaskStatus.COMPLETED.value,
            "current_phase": "completed",
            "pause_marker": None,
        })
        # 显式:不重试 → finalize_task
    elif decision == "retry":
        new_state.update({
            "format_loop_count": int(state.get("format_loop_count") or 0) + 1,
            "task_status": TaskStatus.EXPORTING.value,
            "current_phase": "post_confirm",
            "pause_marker": None,
        })
        # 显式:重试 → prepare_export(由条件边 route_after_format_check 处理)
    else:
        new_state.update({
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "pause_marker": None,
        })

    return new_state


# ── generate_completion_summary (Phase 2.8D) ──────────────────────────────────


SUMMARY_SYSTEM_PROMPT: str = (
    "你是 TestAgent 的任务完成摘要助手。基于提供的任务事实，用中文输出 2 至 4 句简洁的完成总结。"
    "说明已完成的工作、生成结果和需要用户关注的审查建议。不要使用标题、Markdown 或编造事实。"
)


async def _build_summary_facts(state: TestPlanGraphState) -> dict:
    """Phase 2.8D:从 state 中提取摘要需要的事实(镜像 Legacy L341-357)。"""
    plan = state.get("test_plan_content") if isinstance(state.get("test_plan_content"), dict) else {}
    review = state.get("review_result") if isinstance(state.get("review_result"), dict) else {}
    artifact = state.get("artifact") if isinstance(state.get("artifact"), dict) else {}
    return {
        "user_instruction": state.get("user_prompt") or "",
        "generation": {
            "generated_sections": plan.get("generated_sections", 0),
            "kept_sections": plan.get("kept_sections", 0),
            "manual_sections": plan.get("manual_sections", 0),
        },
        "review": {
            "passed": review.get("passed"),
            "level": review.get("level"),
            "suggestions": list(review.get("suggestions") or [])[:3],
        },
        "artifact_name": artifact.get("file_name") or artifact.get("name"),
    }


def _fallback_summary(state: TestPlanGraphState) -> str:
    """Phase 2.8D:LLM 失败 / 无 settings_service 时的模板兜底(镜像 Legacy L377-385)。"""
    plan = state.get("test_plan_content") if isinstance(state.get("test_plan_content"), dict) else {}
    artifact = state.get("artifact") if isinstance(state.get("artifact"), dict) else {}
    generated = int(plan.get("generated_sections") or 0)
    kept = int(plan.get("kept_sections") or 0)
    parts = ["任务已完成。"]
    if generated or kept:
        parts.append(f"已生成 {generated} 个章节，保留 {kept} 个模板章节。")
    if artifact.get("file_name") or artifact.get("name"):
        parts.append(f"已生成产物：{artifact.get('file_name') or artifact.get('name')}。")
    return "".join(parts)


async def _resolve_config_provider(settings_service: Any, user_internal_id: int) -> Any:
    """Phase 2.8D:从 SettingsService 取 LLMConfigProvider;兼容 sync/async 形态。

    SettingsService.build_llm_config_provider(user_id) 是 async(Phase 2.0+)，
    但 LLMClient(config_provider=...) 期望 attribute-based provider。Legacy 路径
    `LLMClient(config_provider=ctx.settings_service)` 实际上因为 attribute 查找失
    败而走 fallback 模板；这里明确以 await 形式取 cfg。
    """
    if settings_service is None:
        return None
    builder = getattr(settings_service, "build_llm_config_provider", None)
    if builder is None and hasattr(settings_service, "llm_config_provider"):
        builder = settings_service.llm_config_provider
    if callable(builder):
        cfg = builder(user_internal_id) if "user_internal_id" in builder.__code__.co_varnames else builder()
        if inspect.isawaitable(cfg):
            cfg = await cfg
        return cfg
    return settings_service


async def generate_completion_summary_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """Phase 2.8D:复用 Legacy ``_generate_completion_summary`` 逻辑。

    * 字节级镜像 Legacy L339-385 主体
    * LLM 失败 / settings_service 缺 → fallback 模板(沿用 Legacy)
    * 写 ``state["summary"]`` 给 finalize_task 节点读
    """
    completed = mark_completed(state, NODE_GENERATE_COMPLETION_SUMMARY)
    settings_service = getattr(ctx, "settings_service", None)
    facts = await _build_summary_facts(state)
    summary_text: str | None = None

    if settings_service is not None:
        try:
            cfg_provider = await _resolve_config_provider(settings_service, ctx.user_internal_id)
            if cfg_provider is not None:
                # Frozen v2 tasks may be resumed for history compatibility,
                # but may not revive a Legacy LLM prompt.  The final summary
                # below is deterministic for this historical graph.
                summary_text = None
        except Exception as exc:  # noqa: BLE001 - completion summary 是非关键路径
            logger.warning(
                "generate_completion_summary_node: LLM failed | task_id=%s | error=%s",
                ctx.task_internal_id,
                type(exc).__name__,
            )

    if not summary_text:
        summary_text = _fallback_summary(state)

    # Phase 2.8D:TOOL_FINISHED 走 helper 嵌 chunk 流(向后兼容 2.5/2.6 SSE 协议)
    await _emit_with_public_update(
        task_id=str(ctx.task_internal_id),
        node_name=NODE_GENERATE_COMPLETION_SUMMARY,
        event_type=AgentEventType.TOOL_FINISHED.value,
        payload={"tool_name": "completion_summary", "summary": summary_text},
        ctx=ctx,
        tool_name="completion_summary",
        success=True,
        elapsed_ms=20_000,
    )

    return {
        "summary": summary_text,
        "current_node": NODE_GENERATE_COMPLETION_SUMMARY,
        "completed_nodes": completed,
    }


# ── finalize_task ────────────────────────────────────────────────────────────


async def finalize_task_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """TASK_COMPLETED;若带 artifact + summary → emit 收尾。

    Phase 2.8D:读 ``state["summary"]``(由 generate_completion_summary_node 写入)塞
    TASK_COMPLETED payload,与 Legacy ``ctx.completion_summary`` 同名同语义。
    """
    completed = mark_completed(state, NODE_FINALIZE_TASK)
    artifact = state.get("artifact") or {}
    summary_text = state.get("summary")

    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_FINALIZE_TASK,
        event_type=AgentEventType.ARTIFACT_CREATED.value,
        title="产物已生成",
        content="",
        payload={"artifact": artifact},
    )
    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_FINALIZE_TASK,
        event_type=AgentEventType.TASK_COMPLETED.value,
        title="任务已完成",
        content="",
        payload={"summary": summary_text},
    )

    return {
        "task_status": TaskStatus.COMPLETED.value,
        "current_phase": "completed",
        "current_node": NODE_FINALIZE_TASK,
        "completed_nodes": completed,
    }


__all__ = [
    "NODE_RESUME_TASK",
    "NODE_GENERATE_TEST_PLAN",
    "NODE_PREPARE_EXPORT",
    "NODE_EXPORT_WORD",
    "NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION",
    "NODE_RECORD_LOSS_DECISION",
    "NODE_FINALIZE_TASK",
    # Phase 2.8D
    "NODE_GENERATE_COMPLETION_SUMMARY",
    "SUMMARY_SYSTEM_PROMPT",
    "resume_task_node",
    "generate_test_plan_node",
    "prepare_export_node",
    "export_word_node",
    "pause_for_legacy_format_decision_node",
    "record_loss_decision_node",
    "generate_completion_summary_node",
    "finalize_task_node",
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
