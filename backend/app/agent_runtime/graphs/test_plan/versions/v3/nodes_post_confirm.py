"""v2 post-confirm 节点 (Legacy orchestrator resume_after_confirm 段)。

对应业务段:从 SectionConfirmationInterrupt(或 Legacy sentinel pause)resume 后,
到生成测试方案 + 审查(隐式在 review_format 节点)+ 导出 Word + 二审 + 最终
收尾这一完整链。

节点(按执行顺序):
* ``resume_task``                      — 真 interrupt resume 后第一个节点,确认状态
* ``generate_test_plan``               — 调 TestPlanGeneratorTool 出 section_package + retry
* ``prepare_export``                   — 准备 artifact 元数据(public_id 等)
* ``export_word``                      — 调 WordExportTool 模板回填,失败/格式损失走二级 interrupt
* ``pause_for_legacy_format_decision`` — LEGACY sentinel pause(默认不开)
* ``record_loss_decision``             — format_loss review 决策落库
* ``generate_completion_summary_node`` — Phase 2.8D 复用 Legacy 摘要生成
* ``finalize_task``                    — TASK_COMPLETED 收尾

所有节点遵循 ``async def node_xxx(state, *, ctx) -> dict`` 业务签名,
通过 ``_bind_async`` 包装后被 graph.add_node 注册;状态字段完整变更在
各节点的 return dict 内。
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Dict, Optional

from app.agent.enums import AgentEventType, TaskStatus
from app.agent.retry_policy import RetryContext

from app.agent_runtime.runtime_context import RuntimeContext

from app.agent_runtime.graphs.nodes_emit_helper import _emit_with_public_update
from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState
from app.agent_runtime._shared.artifact_contract import (
    ExportArtifactErrorCode,
    is_artifact_present,
    normalize_export_artifact,
)
from app.agent_runtime._shared.summary_facts import (
    build_summary_facts,
    fallback_summary_text,
)
from .nodes_util import mark_completed
from .nodes_pre_confirm import _build_pending_narrative
from .routing import (
    NODE_FAIL_TASK,
    NODE_REVIEW_STEP,
    route_after_export_word,
    route_after_generate_test_plan,
)
from .routing_after_interrupt import NODE_FORMAT_LOSS_INTERRUPT

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


# ── Phase 2.9A.11: TestPlanGeneratorTool 输出规范化 ────────────────


# 真实 LLM 输出的字段名(历史/兼容)
_TEST_PLAN_CONTENT_FIELDS = (
    "content",
    "result",
    "test_plan",
    "generated_content",
    "text",
)


def _extract_section_content_length(value: Any) -> int:
    """从 generated_sections[i].content(可能是 str / list[dict] / dict)提取可读文本长度。

    Phase 2.9A.11 真实结构审计:
      * ``content`` 字段常见类型:
        - ``str``            — 纯文本段落
        - ``list[dict]``     — 多表格行(每行 dict)
        - ``list[list[dict]]``— 多表格(outer per table)
        - ``dict``           — 单表格(legacy format)
    累计可读字符长度(剥除空白后)用于校验生成节点是否真的产出内容。
    """
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.strip())
    if isinstance(value, list):
        total = 0
        for item in value:
            total += _extract_section_content_length(item)
        return total
    if isinstance(value, dict):
        # 单行表格行 — 累加 value
        total = 0
        for v in value.values():
            if isinstance(v, str):
                total += len(v.strip())
            elif isinstance(v, (list, dict)):
                total += _extract_section_content_length(v)
        return total
    return 0


def _extract_section_title(section: Dict[str, Any]) -> str:
    """从 generated_sections[i] 提取章节标题(多种兼容字段名)。"""
    if not isinstance(section, dict):
        return ""
    for field_name in ("title", "clean_title", "name", "section_title"):
        v = section.get(field_name)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _extract_section_id(section: Dict[str, Any]) -> str:
    """从 generated_sections[i] 提取章节 ID(多种兼容字段名)。"""
    if not isinstance(section, dict):
        return ""
    for field_name in ("section_id", "id", "field"):
        v = section.get(field_name)
        if v is not None and str(v):
            return str(v)
    return ""


def normalize_test_plan_generation_output(
    envelope: Dict[str, Any],
) -> Dict[str, Any]:
    """Phase 2.9A.11:TestPlanGeneratorTool 输出的规范化函数。

    输入:
      * ``envelope`` — Adapter.execute() 返回的整封信封(含 success /
        data / error / duration_ms 等)。

    输出 — 统一 6 字段结构(供 generate_test_plan_node + ResultReviewTool):

      {
        "schema_variant": "section_package_v1" | "top_level_v1" | "legacy",
        "section_package": Dict,
        "generated_sections": List[Dict],
        "kept_sections": List[Dict],
        "manual_sections": List[Dict],
        "total_word_count": int,
        "tables_generated": int,
        "normalized_section_count": int,
        "non_empty_section_count": int,
        "normalized_content_length": int,
      }

    兼容多种 schema 变体:
      * ``section_package.generated_sections`` — F019 标准(via build_section_package)
      * 顶层 ``generated_sections`` — 历史/手工 envelope
      * ``data`` 直接是 List[Dict] — 兜底

    所有 schema 兼容逻辑必须集中在本函数中;节点 / Adapter /
    ResultReviewTool 不得各自再做字段名兼容。
    """
    result: Dict[str, Any] = {
        "schema_variant": "unknown",
        "section_package": {},
        "generated_sections": [],
        "kept_sections": [],
        "manual_sections": [],
        "total_word_count": 0,
        "tables_generated": 0,
        "normalized_section_count": 0,
        "non_empty_section_count": 0,
        "normalized_content_length": 0,
    }

    if not isinstance(envelope, dict):
        return result

    data = envelope.get("data") if envelope.get("success") else envelope.get("data")
    if data is None and envelope.get("error") is None:
        # envelope 失败时 data 可能是 None — 用 envelope.data 即便 envelope.success=False
        data = envelope.get("data")
    if not isinstance(data, dict):
        return result

    # 1. 优先取 section_package 路径(F019 标准)
    section_package = data.get("section_package")
    if isinstance(section_package, dict):
        result["schema_variant"] = "section_package_v1"
        result["section_package"] = section_package
        generated = section_package.get("generated_sections")
        if isinstance(generated, list):
            result["generated_sections"] = generated
        keep = section_package.get("keep_sections")
        if isinstance(keep, list):
            result["kept_sections"] = keep
        manual = section_package.get("manual_sections")
        if isinstance(manual, list):
            result["manual_sections"] = manual
    else:
        # 2. 顶层 generated_sections 路径(历史变体)
        generated = data.get("generated_sections")
        if isinstance(generated, list):
            result["schema_variant"] = "top_level_v1"
            result["generated_sections"] = generated
            keep = data.get("kept_sections")
            if isinstance(keep, list):
                result["kept_sections"] = keep
            manual = data.get("manual_sections")
            if isinstance(manual, list):
                result["manual_sections"] = manual

    # 3. 统计指标
    total_word_count = data.get("total_word_count")
    if isinstance(total_word_count, int):
        result["total_word_count"] = total_word_count
    elif isinstance(total_word_count, str) and total_word_count.isdigit():
        result["total_word_count"] = int(total_word_count)

    tables_generated = data.get("tables_generated")
    if isinstance(tables_generated, int):
        result["tables_generated"] = tables_generated
    elif isinstance(tables_generated, str) and tables_generated.isdigit():
        result["tables_generated"] = int(tables_generated)

    # 3b. schema_issues 透传 — Phase 2.9A.X
    # TestPlanGeneratorTool 把表头不符 / 截断缺失字段写到
    # envelope.data.schema_issues；ResultReviewTool 7c 段需要从这里读。
    # 之前没复制导致 normalize 后 schema_issues 被丢掉，ResultReviewTool
    # 永远读到 None，7c 段从不触发，审查永远通过。
    schema_issues = data.get("schema_issues")
    if isinstance(schema_issues, list) and schema_issues:
        result["schema_issues"] = schema_issues

    # 3c. generation_recovery 透传 — JSON 截断不是普通局部缺章。
    # TestPlanGeneratorTool 会把截断恢复策略写在 generation_recovery 中，
    # RepairAgent 依赖这个元数据绕过普通“每轮最多 3 章”的 LLM 决策路径，
    # 改走确定性的批量恢复。normalize 若丢掉它，截断会被误判成普通缺章。
    generation_recovery = data.get("generation_recovery")
    if isinstance(generation_recovery, dict) and generation_recovery:
        result["generation_recovery"] = generation_recovery

    # 4. 章节级统计 — 每章必须有 title 或 section_id + 至少 1 章有非空正文
    chapter_count = 0
    non_empty_count = 0
    total_length = 0
    for sec in result["generated_sections"]:
        if not isinstance(sec, dict):
            continue
        title = _extract_section_title(sec)
        section_id = _extract_section_id(sec)
        if not title and not section_id:
            # 章节必须有标题或 ID,否则视为无效 — 不计入统计
            continue
        chapter_count += 1
        # 内容长度 — 支持 str / list[dict] / dict
        content_length = _extract_section_content_length(sec.get("content"))
        if content_length > 0:
            non_empty_count += 1
        total_length += content_length

    result["normalized_section_count"] = chapter_count
    result["non_empty_section_count"] = non_empty_count
    result["normalized_content_length"] = total_length

    return result


def _adapter(ctx: RuntimeContext):
    return getattr(ctx, "tool_adapter", None)


def _terminal_event_id(adapter: Any) -> str:
    if adapter is None or not hasattr(adapter, "last_terminal_event_id"):
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


def _core_failed(envelope: Dict[str, Any]) -> bool:
    """A failed tool envelope must never be normalized as generated content.

    ``recoverable`` means a caller may retry the *tool*, not that the current
    graph invocation produced a valid test plan. Letting a recoverable error
    fall through rewrites its code as ``GENERATION_OUTPUT_INVALID`` and hides
    the real operational cause from both the UI and task audit trail.
    """
    return not bool(envelope.get("success"))


def _needs_generation_recovery(envelope: Dict[str, Any]) -> bool:
    """Identify parser failures that must get one CE-only recovery pass.

    The normal path is handled inside ``TestPlanGeneratorTool``. This narrow
    guard protects a stale or misconfigured bridge from turning an otherwise
    recoverable parser error into a terminal graph state before that path can
    run. Authentication, quota, and provider availability errors are not in
    this allow-list and remain terminal here.
    """
    error = envelope.get("error") or {}
    if not isinstance(error, dict) or not error.get("recoverable"):
        return False
    code = str(error.get("code") or "")
    return code == "JSON_VALIDATION_FAILED" or code.endswith("LLMProfileParseError")


# ── resume_task ──────────────────────────────────────────────────────────────


async def resume_task_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """进入 post_confirm 段。用户决策已经被 coordinator 写进 ``section_confirm_config``。

    Phase 2.9A.9:从 section_confirm_config.sections 派生 confirmed_sections
    写到 Graph State,供 TestPlanGeneratorTool 启动前校验。
    """
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

    section_confirm_config = state.get("section_confirm_config") or {}
    confirmed_sections = (
        section_confirm_config.get("sections")
        if isinstance(section_confirm_config, dict) else None
    )

    return {
        "pause_marker": None,
        "current_phase": "post_confirm",
        "task_status": TaskStatus.GENERATING.value,
        "current_node": NODE_RESUME_TASK,
        "completed_nodes": completed,
        "confirmed_sections": (
            confirmed_sections if isinstance(confirmed_sections, list) else None
        ),
    }


# ── generate_test_plan ───────────────────────────────────────────────────────


async def generate_test_plan_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """调 TestPlanGeneratorTool(由 adapter 处理 retry 与 pacing)。

    Phase 2.9A.9:Resume 后 Graph State 由 LangGraph checkpointer 恢复,
    必须先校验 4 个业务字段非空,缺失时抛 ``GENERATION_STATE_MISSING``
    并 emit STAGE_FAILED;不调 TestPlanGeneratorTool,避免级联 MISSING_REQUIREMENT。
    """
    completed = mark_completed(state, NODE_GENERATE_TEST_PLAN)
    adapter = _adapter(ctx)

    # ── Phase 2.9A.9: Resume 前状态验证
    # 来源:Graph State(由 LangGraph checkpointer 持久化/恢复)
    requirement_analysis = state.get("requirement_analysis") or {}
    template_structure = state.get("template_structure") or {}
    section_suggestions = state.get("section_suggestions") or {}
    section_confirm_config = state.get("section_confirm_config") or {}

    # confirmed_sections 既可能在顶层(Phase 2.9A.9 新增),也可能在
    # section_confirm_config.sections 里 — 两处都读。
    confirmed_sections = state.get("confirmed_sections")
    if not confirmed_sections and isinstance(section_confirm_config, dict):
        confirmed_sections = section_confirm_config.get("sections")

    # 严格 missing 校验:仅当 Graph State 中字段为 None(从未写入,即
    # LangGraph Checkpoint 没保存过)或键不存在时,才视为缺失。
    # 空 dict {} 视为"已写入但 stub 没产出" — 允许继续(Phase 2.1 测试
    # 兼容)。
    # 例外:``confirmed_sections`` 必须是 list 且非空(空 list = 用户
    # 在 UI 上一节都没确认,等于没决定,不调用生成工具)。
    missing_fields: list[str] = []
    if "requirement_analysis" not in state or state["requirement_analysis"] is None:
        missing_fields.append("requirement_analysis")
    if "template_structure" not in state or state["template_structure"] is None:
        missing_fields.append("template_structure")
    if "section_suggestions" not in state or state["section_suggestions"] is None:
        missing_fields.append("section_suggestions")
    if (
        confirmed_sections is None
        or not isinstance(confirmed_sections, list)
        or len(confirmed_sections) == 0
    ):
        missing_fields.append("confirmed_sections")

    if missing_fields:
        logger.error(
            "Phase 2.9A.9 generate_test_plan: Graph State 缺失业务字段 | "
            "task_internal_id=%s | missing=%s | "
            "generation_state_keys=%s | requirement_present=%s | "
            "template_present=%s | confirmed_sections_count=%s",
            ctx.task_internal_id,
            missing_fields,
            list(state.keys()),
            bool(requirement_analysis),
            bool(template_structure),
            len(confirmed_sections) if isinstance(confirmed_sections, list) else 0,
        )
        await ctx.event_sink.emit(
            task_id=str(ctx.task_internal_id),
            graph_run_id=f"run-{ctx.task_internal_id}",
            node_name=NODE_GENERATE_TEST_PLAN,
            event_type=AgentEventType.STAGE_FAILED.value,
            title="生成阶段 Graph State 缺失",
            content="",
            payload={
                "code": "GENERATION_STATE_MISSING",
                "missing_fields": missing_fields,
            },
        )
        return {
            "last_error": {
                "code": "GENERATION_STATE_MISSING",
                "message": (
                    f"Graph State 缺失必需业务字段: {missing_fields};"
                    " Resume 后 LangGraph Checkpoint 未恢复这些字段,"
                    " 拒绝继续调用 TestPlanGeneratorTool"
                ),
            },
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_GENERATE_TEST_PLAN,
            "completed_nodes": completed,
            "next_node": NODE_FAIL_TASK,
        }

    if adapter is None:
        return {
            "test_plan_content": {},
            "current_node": NODE_GENERATE_TEST_PLAN,
            "completed_nodes": completed,
            "next_node": NODE_REVIEW_STEP,
        }

    tool_inputs = {
        "requirement_analysis": requirement_analysis,
        "template_structure": template_structure,
        "section_confirm_config": section_confirm_config,
        "knowledge_search_result": state.get("knowledge_search_result") or {},
        "retrieval_evidence_bundle": state.get("retrieval_evidence_bundle") or {},
    }
    envelope = await adapter.execute(
        tool_name="TestPlanGeneratorTool",
        inputs=tool_inputs,
        ctx_runtime=ctx,
        graph_state=dict(state) if state else None,
    )

    if _needs_generation_recovery(envelope):
        original_error = dict(envelope.get("error") or {})
        logger.warning(
            "generate_test_plan: recoverable parser failure; rerunning through "
            "the CE-only tool recovery protocol | task_internal_id=%s | code=%s",
            ctx.task_internal_id,
            original_error.get("code"),
        )
        await adapter.emit_retry_event(
            tool_name="TestPlanGeneratorTool",
            strategy="schema_feedback",
            attempt=2,
            ctx_runtime=ctx,
            last_error=str(original_error.get("message") or "")[:500],
            next_attempt_in_seconds=0,
        )
        envelope = await adapter.execute(
            tool_name="TestPlanGeneratorTool",
            inputs=tool_inputs,
            ctx_runtime=ctx,
            attempt=2,
            retry_context=RetryContext(
                attempt=2,
                max_retries=2,
                previous_errors=[original_error],
                strategy="schema_feedback",
            ),
            graph_state=dict(state) if state else None,
        )

    if _core_failed(envelope):
        # Phase 2.9A.9:生成失败 → 明确 last_error + status=failed。
        # 不依赖下游 conditional edge,这里直接返回失败状态,
        # route_after_generate_test_plan 走 fail_task 而非 review/export。
        logger.error(
            "Phase 2.9A.9 generate_test_plan: tool failed | "
            "task_internal_id=%s | error=%s",
            ctx.task_internal_id,
            envelope.get("error"),
        )
        await ctx.event_sink.emit(
            task_id=str(ctx.task_internal_id),
            graph_run_id=f"run-{ctx.task_internal_id}",
            node_name="TestPlanGeneratorTool",  # Phase 2.9A.11:节点名用真实工具名,避免前端显示通用 AgentTool
            event_type=AgentEventType.STAGE_FAILED.value,
            title="测试方案生成失败",
            content="",
            payload={
                "tool_name": "TestPlanGeneratorTool",  # Phase 2.9A.11:前端以此显示工具身份
                "tool_call_id": f"TestPlanGeneratorTool-{ctx.task_internal_id}",
                "failed_stage": "generate_test_plan",
                "error": envelope.get("error"),
            },
        )
        return _with_pending_narrative({
            "last_error": envelope.get("error") or {"code": "GENERATION_FAILED"},
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_GENERATE_TEST_PLAN,
            "completed_nodes": completed,
        },
            state=state,
            tool_name="TestPlanGeneratorTool",
            tool_call_id=str(envelope.get("tool_call_id") or ""),
            attempt=int(envelope.get("attempt") or 1),
            terminal_status="failed",
            continuation_route=NODE_FAIL_TASK,
            duration_ms=envelope.get("duration_ms"),
            source_event_id=_terminal_event_id(adapter),
        )

    # ── Phase 2.9A.11: 规范化 + 校验
    # envelope.data → 统一 6 字段结构(section_package / generated_sections /
    # kept_sections / manual_sections / total_word_count / tables_generated)
    # 加上统计字段(normalized_section_count / non_empty_section_count /
    # normalized_content_length)。
    normalized = normalize_test_plan_generation_output(envelope)

    chapter_count = int(normalized.get("normalized_section_count") or 0)
    non_empty_count = int(normalized.get("non_empty_section_count") or 0)
    normalized_content_length = int(normalized.get("normalized_content_length") or 0)
    declared_total_word_count = int(normalized.get("total_word_count") or 0)
    schema_variant = str(normalized.get("schema_variant") or "unknown")
    schema_issues = normalized.get("schema_issues")
    has_reviewable_schema_issues = (
        isinstance(schema_issues, list)
        and any(isinstance(issue, dict) for issue in schema_issues)
    )

    # 校验失败判定 — Phase 2.9A.11 严格语义
    # 至少 1 章有标题或 section_id,且至少 1 章正文非空(剥除空白)。
    #
    # 例外：TestPlanGeneratorTool 对截断/字段不符会返回 success +
    # schema_issues，让 ResultReviewTool/RepairAgent 接管。这类结果虽然
    # 可能没有任何可读章节，但它是"可审查的坏结果"，不能在生成节点
    # fail-fast，否则会跳过 3.0 的 review -> repair 闭环。
    if (
        not has_reviewable_schema_issues
        and (chapter_count == 0 or non_empty_count == 0 or normalized_content_length == 0)
    ):
        logger.error(
            "Phase 2.9A.11 generate_test_plan: 内容为空 | "
            "task_internal_id=%s | schema_variant=%s | "
            "normalized_section_count=%s | non_empty_section_count=%s | "
            "normalized_content_length=%s | declared_total_word_count=%s",
            ctx.task_internal_id,
            schema_variant,
            chapter_count,
            non_empty_count,
            normalized_content_length,
            declared_total_word_count,
        )
        await ctx.event_sink.emit(
            task_id=str(ctx.task_internal_id),
            graph_run_id=f"run-{ctx.task_internal_id}",
            node_name=NODE_GENERATE_TEST_PLAN,
            event_type=AgentEventType.STAGE_FAILED.value,
            title="测试方案内容为空",
            content="",
            payload={
                "code": "GENERATION_OUTPUT_INVALID",
                "tool_name": "TestPlanGeneratorTool",
                "failed_stage": "generate_test_plan",
                "schema_variant": schema_variant,
                "normalized_section_count": chapter_count,
                "non_empty_section_count": non_empty_count,
                "normalized_content_length": normalized_content_length,
                "declared_total_word_count": declared_total_word_count,
            },
        )
        return _with_pending_narrative({
            "last_error": {
                "code": "GENERATION_OUTPUT_INVALID",
                "message": (
                    f"TestPlanGeneratorTool 输出无有效正文 "
                    f"(schema_variant={schema_variant}, "
                    f"normalized_section_count={chapter_count}, "
                    f"non_empty_section_count={non_empty_count}, "
                    f"normalized_content_length={normalized_content_length}, "
                    f"declared_total_word_count={declared_total_word_count}); "
                    "生成节点拒绝继续调用 ResultReviewTool"
                ),
            },
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_GENERATE_TEST_PLAN,
            "completed_nodes": completed,
        },
            state=state,
            tool_name="TestPlanGeneratorTool",
            tool_call_id=str(envelope.get("tool_call_id") or ""),
            attempt=int(envelope.get("attempt") or 1),
            terminal_status="failed",
            continuation_route=NODE_FAIL_TASK,
            duration_ms=envelope.get("duration_ms"),
            source_event_id=_terminal_event_id(adapter),
        )

    if has_reviewable_schema_issues and (
        chapter_count == 0 or non_empty_count == 0 or normalized_content_length == 0
    ):
        logger.warning(
            "Phase 2.9A.11 generate_test_plan: empty/partial content allowed "
            "because schema_issues are reviewable | task_internal_id=%s | "
            "schema_variant=%s | issue_count=%s | normalized_section_count=%s | "
            "non_empty_section_count=%s | normalized_content_length=%s",
            ctx.task_internal_id,
            schema_variant,
            len(schema_issues),
            chapter_count,
            non_empty_count,
            normalized_content_length,
        )

    logger.info(
        "Phase 2.9A.11 generate_test_plan: generation completed | "
        "task_internal_id=%s | tool_name=TestPlanGeneratorTool | "
        "schema_variant=%s | normalized_section_count=%s | "
        "non_empty_section_count=%s | normalized_content_length=%s | "
        "declared_total_word_count=%s | checkpoint_write_pending=true",
        ctx.task_internal_id,
        schema_variant,
        chapter_count,
        non_empty_count,
        normalized_content_length,
        declared_total_word_count,
    )

    # Phase 2.9A.11:Graph State 写入规范化结构(而非 envelope.data 原始 dict)
    # ResultReviewTool 必须从同一规范化结构读取 — 兼容逻辑只在
    # normalize_test_plan_generation_output 集中处理。
    patch = {
        "test_plan_content": normalized,
        "review_loop_count": 0,
        "current_node": NODE_GENERATE_TEST_PLAN,
        "completed_nodes": completed,
    }
    continuation_route = route_after_generate_test_plan({**dict(state), **patch})
    return _with_pending_narrative(
        patch,
        state=state,
        tool_name="TestPlanGeneratorTool",
        tool_call_id=str(envelope.get("tool_call_id") or ""),
        attempt=int(envelope.get("attempt") or 1),
        terminal_status="success",
        continuation_route=continuation_route,
        duration_ms=envelope.get("duration_ms"),
        source_event_id=_terminal_event_id(adapter),
    )


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
            "next_node": route_after_export_word(state),
        }

    loops = int(state.get("format_loop_count") or 0)
    fidelity = {1: "medium", 2: "low"}.get(loops + 1, "low")

    envelope = await adapter.execute(
        tool_name="WordExportTool",
        inputs={
            "test_plan_content": state.get("test_plan_content") or {},
            "template_structure": state.get("template_structure") or {},
            "fidelity": fidelity,
            # Phase 2.9A.14: 显式传递 template_file_id, 避免 _build_proxy
            # 从空 inputs 中取到 None, 导致 WordExportTool 收到
            # empty_reference 报 EXPORT_TEMPLATE_NOT_FOUND。
            "template_file_id": state.get("template_file_id"),
        },
        ctx_runtime=ctx,
        graph_state=dict(state) if state else None,
    )

    if _core_failed(envelope):
        # Phase 2.9A.13: 导出失败不重置 test_plan_content / review_result。
        # 这些是上游已经成功的产物(生成 16 章节,审查通过),不能因为下游
        # 导出失败就把整个任务展示成"未生成任何内容"。下游摘要事实
        # 来源:test_plan_content(章节统计)/ review_result(审查统计)/
        # artifact(本次未生成)。前端的"生成结果摘要"卡片应从 state 直接
        # 读,而不是绑定 artifact 是否存在。
        last_error = envelope.get("error") or {"code": "WORD_EXPORT_FAILED"}
        return _with_pending_narrative({
            "last_error": last_error,
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_EXPORT_WORD,
            "completed_nodes": completed,
            # 保留: test_plan_content / review_result / template_structure /
            # requirement_analysis / knowledge_search_result — 之前的 Graph
            # State 已固化,这里只追加 status 标记,不会清空已成功字段。
            "export_status": "failed",
            "export_failure_code": last_error.get("code"),
        },
            state=state,
            tool_name="WordExportTool",
            tool_call_id=str(envelope.get("tool_call_id") or ""),
            attempt=int(envelope.get("attempt") or 1),
            terminal_status="failed",
            continuation_route=NODE_FAIL_TASK,
            duration_ms=envelope.get("duration_ms"),
            source_event_id=_terminal_event_id(adapter),
        )

    # Phase 2.9A.16: 只从 envelope.data 提取 Artifact。
    # Graph v3 不再依赖 context.proxied side-effect；
    # WordExportTool 显式在 data 中返回 storage_path。
    # Phase 2.9A.17: 走 normalize_export_artifact 统一规范:
    # - 字段白名单 + JSON-safe 校验
    # - 失败:errors 列表收集多个错误码,不映射成字符串 magic
    # - canonical artifact 进入 state["artifact"],供 check_docx_format_node 读
    raw_tool_data = envelope.get("data") or {}
    pending_losses = (
        raw_tool_data.get("pending_format_losses")
        if isinstance(raw_tool_data, dict)
        else []
    )
    if not isinstance(pending_losses, list):
        pending_losses = []
    format_loss_pending = bool(
        isinstance(raw_tool_data, dict)
        and raw_tool_data.get("format_loss_pending")
        and pending_losses
    )

    normalized = normalize_export_artifact(raw_tool_data)
    if not normalized.ok:
        # Phase 2.9A.17: 错误码不再压缩成单一字符串 — 透传 errors 让
        # downstream / 诊断日志看到具体缺哪几个字段。
        last_error = {
            "code": ExportArtifactErrorCode.STATE_INVALID,
            "message": (
                "ExportTool 返回的 Artifact 不满足 Graph State 契约: "
                + "; ".join(normalized.errors)
            ),
            "details": {
                "errors": list(normalized.errors),
                "warnings": list(normalized.warnings),
            },
        }
        logger.error(
            "export_word_node: artifact 规范化失败 | "
            "task_internal_id=%s | errors=%s | warnings=%s",
            ctx.task_internal_id,
            normalized.errors,
            normalized.warnings,
        )
        return _with_pending_narrative({
            "last_error": last_error,
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_EXPORT_WORD,
            "completed_nodes": completed,
            "export_status": "failed",
            "export_failure_code": last_error["code"],
        },
            state=state,
            tool_name="WordExportTool",
            tool_call_id=str(envelope.get("tool_call_id") or ""),
            attempt=int(envelope.get("attempt") or 1),
            terminal_status="failed",
            continuation_route=NODE_FAIL_TASK,
            duration_ms=envelope.get("duration_ms"),
            source_event_id=_terminal_event_id(adapter),
        )
    raw_artifact = normalized.artifact  # canonical dict
    if not raw_artifact.get("artifact_id"):
        # 兼容老 envelope 形态 — canonical 必有 public_id;没 public_id 即错误
        return _with_pending_narrative({
            "last_error": {"code": "EXPORT_ARTIFACT_INVALID", "message": "导出产物数据无效"},
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_EXPORT_WORD,
            "completed_nodes": completed,
        },
            state=state,
            tool_name="WordExportTool",
            tool_call_id=str(envelope.get("tool_call_id") or ""),
            attempt=int(envelope.get("attempt") or 1),
            terminal_status="failed",
            continuation_route=NODE_FAIL_TASK,
            duration_ms=envelope.get("duration_ms"),
            source_event_id=_terminal_event_id(adapter),
        )
    if not raw_artifact.get("storage_path"):
        return _with_pending_narrative({
            "last_error": {"code": "EXPORT_ARTIFACT_STATE_INVALID", "message": "Artifact 缺少 storage_path"},
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_EXPORT_WORD,
            "completed_nodes": completed,
        },
            state=state,
            tool_name="WordExportTool",
            tool_call_id=str(envelope.get("tool_call_id") or ""),
            attempt=int(envelope.get("attempt") or 1),
            terminal_status="failed",
            continuation_route=NODE_FAIL_TASK,
            duration_ms=envelope.get("duration_ms"),
            source_event_id=_terminal_event_id(adapter),
        )
    logger.info(
        "export_word_node: artifact_source=envelope | public_id=%s | storage_path_present=%s | file_size=%s | mime_type=%s",
        raw_artifact.get("public_id"),
        bool(raw_artifact.get("storage_path")),
        raw_artifact.get("file_size"),
        raw_artifact.get("mime_type"),
    )
    # Phase 2.9A.X: secondary 结构丢失(书签 / 批注 / 脚注 / 尾注)→ 走
    # ``format_loss_review`` 让用户决定继续 / 重试 / 放弃,而**不直接 fail_task**。
    # 即便 medium fidelity 已"容忍",设计预期是用户应被告知文件结构有问题。
    if format_loss_pending:
        from app.agent.enums import TaskStatus as _TaskStatus
        patch = {
            "artifact": raw_artifact,
            "checked_artifact_public_id": raw_artifact.get("public_id"),
            "export_status": "loss_review",
            "last_retry_strategy": None,
            "pause_marker": "format_loss_review",
            "pending_format_losses": pending_losses,
            "task_status": _TaskStatus.FORMAT_LOSS_REVIEW.value,
            "current_node": NODE_EXPORT_WORD,
            "completed_nodes": completed,
        }
        try:
            await ctx.event_sink.emit(
                task_id=str(ctx.task_internal_id),
                graph_run_id=f"run-{ctx.task_internal_id}",
                node_name=NODE_EXPORT_WORD,
                event_type=AgentEventType.FORMAT_LOSS_CONFIRM_REQUESTED.value,
                title="导出后格式丢失需要确认",
                content="",
                payload={"losses": pending_losses},
            )
        except Exception:
            logger.warning(
                "export_word_node: emit format_loss_review event failed | "
                "task_internal_id=%s",
                ctx.task_internal_id,
                exc_info=True,
            )
        logger.warning(
            "export_word_node: format_loss_review pending | "
            "task_internal_id=%s | losses=%s",
            ctx.task_internal_id,
            pending_losses,
        )
        return _with_pending_narrative(
            patch,
            state=state,
            tool_name="WordExportTool",
            tool_call_id=str(envelope.get("tool_call_id") or ""),
            attempt=int(envelope.get("attempt") or 1),
            terminal_status="success",
            continuation_route=NODE_FORMAT_LOSS_INTERRUPT,
            duration_ms=envelope.get("duration_ms"),
            source_event_id=_terminal_event_id(adapter),
        )
    # Phase 2.9A.17: 也写入 checked_artifact_public_id(供后置 format_check
    # 与 routing_after_interrupt 同时读同一标识)。
    patch = {
        "artifact": raw_artifact,
        "checked_artifact_public_id": raw_artifact.get("public_id"),
        "export_status": "succeeded",
        "last_retry_strategy": None,
        "current_node": NODE_EXPORT_WORD,
        "completed_nodes": completed,
    }
    continuation_route = route_after_export_word({**dict(state), **patch})
    return _with_pending_narrative(
        patch,
        state=state,
        tool_name="WordExportTool",
        tool_call_id=str(envelope.get("tool_call_id") or ""),
        attempt=int(envelope.get("attempt") or 1),
        terminal_status="success",
        continuation_route=continuation_route,
        duration_ms=envelope.get("duration_ms"),
        source_event_id=_terminal_event_id(adapter),
    )


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


def _count_sections_from_plan(test_plan_content: Dict[str, Any]) -> tuple[int, int, int]:
    """从 test_plan_content(可能是规范化结构或旧 envelope.data)提取章节数。

    Phase 2.9A.11:normalize 结构下 generated_sections 是 List[Dict];
    旧 envelope.data 下 generated_sections 是 int。
    """
    val = test_plan_content.get("generated_sections")
    if isinstance(val, list):
        generated = len(val)
    else:
        generated = int(val or 0)
    kept = test_plan_content.get("kept_sections")
    if isinstance(kept, list):
        kept_count = len(kept)
    else:
        kept_count = int(kept or 0)
    manual = test_plan_content.get("manual_sections")
    if isinstance(manual, list):
        manual_count = len(manual)
    else:
        manual_count = int(manual or 0)
    return generated, kept_count, manual_count


async def _build_summary_facts(state: TestPlanGraphState) -> dict:
    """Phase 2.8D/2.9A.20:从 state 中提取摘要需要的事实。

    Phase 2.9A.20:委派给 ``_shared.summary_facts`` 的统一构造函数,确保实时 SSE
    与历史 event-list 共用一套事实源,避免两侧不一致。
    """
    canonical = build_summary_facts(dict(state) if state else {})
    # 兼容 finalize_task_node / generate_completion_summary_node payload 字段:
    # LLM 提示词期望 ``generation.generated_sections`` 与 ``review.suggestions``
    # 嵌套格式。这里做最小翻译,业务字段保持原状。
    return {
        "user_instruction": canonical["user_instruction"],
        "generation": {
            "generated_sections": canonical["generated_sections"],
            "kept_sections": canonical["kept_sections"],
            "manual_sections": 0,
        },
        "review": {
            "passed": canonical["review"]["passed"],
            "level": canonical["review"]["level"],
            "suggestions": list(
                (state.get("review_result") or {}).get("suggestions") or []
            )[:3],
            "block_count": canonical["review"]["block_count"],
            "warning_count": canonical["review"]["warning_count"],
        },
        "artifact": {
            "public_id": canonical["artifact"]["public_id"],
            "file_name": canonical["artifact"]["file_name"],
            "page_count": canonical["artifact"].get("page_count"),
        },
        "business_modules": canonical["business_modules"],
        "page_count": canonical.get("page_count"),
    }


def _fallback_summary(state: TestPlanGraphState) -> str:
    """Phase 2.8D/2.9A.20:LLM 失败 / 无 settings_service 时的模板兜底。

    Phase 2.9A.20:委派给 ``summary_facts.fallback_summary_text`` —
    历史回放(刷新 + SSE 重连)和实时事件流共用同一字符串,绝不分叉。
    """
    return fallback_summary_text(dict(state) if state else {})


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

    CE-04:completion summary call-site 迁移(MIG_SUMMARY flag 路由)。
      * flag=false → 保持 legacy(LLMClient + generate_with_system)
      * flag=true  → 只走 ContextInvokerBridge(ctx.context_llm_invoker);
                     失败执行 failure policy(跳过摘要,走 fallback 模板),
                     不静默回退 legacy LLMClient。
    """
    completed = mark_completed(state, NODE_GENERATE_COMPLETION_SUMMARY)
    settings_service = getattr(ctx, "settings_service", None)
    facts = await _build_summary_facts(state)
    summary_text: str | None = None

    # CE-04:MIG_SUMMARY 路由(局部导入避免循环依赖,与 repair/preparation 一致)
    from app.context_engine.feature_flags import require_agent_context_migration
    from app.llm.task_profiles import LLMParserType, LLMTaskProfile

    # CE-05 WP-2：任务路径经 ctx.task_flag_resolver 读 MIG_SUMMARY（冻结 Manifest）
    resolver = getattr(ctx, "task_flag_resolver", None)
    migration_error = require_agent_context_migration(resolver, "MIG_SUMMARY")

    try:
        if migration_error is None:
            # CE-04:MIG_SUMMARY=true → 只走 bridge;失败不静默回退 legacy
            bridge = getattr(ctx, "context_llm_invoker", None)
            if bridge is None or not getattr(bridge, "available", False):
                # failure policy:非关键路径,跳过摘要生成,不调 legacy
                logger.warning(
                    "completion summary MIG_SUMMARY=true 但 Invoker 不可用 | task_id=%s",
                    ctx.task_internal_id,
                )
            else:
                bres = await bridge.generate(
                    user_id=ctx.user_internal_id,
                    call_site="completion.summary",
                    llm_task_profile=LLMTaskProfile(
                        name="completion.summary.v1",
                        system_prompt=SUMMARY_SYSTEM_PROMPT,
                        parser=LLMParserType.PLAIN_TEXT,
                    ),
                    current_node="completion_summary",
                    current_goal=json.dumps(facts, ensure_ascii=False, default=str),
                    task_state_ref={"task_id": str(ctx.task_internal_id)},
                    output_contract="text",
                    user_content=json.dumps(facts, ensure_ascii=False, default=str),
                    conversation_id=ctx.conversation_internal_id,
                    task_id=ctx.task_internal_id,
                    runtime_context=ctx,
                )
                if bres is not None and getattr(bres, "value", None) is not None:
                    summary_text = str(bres.value).strip() or None
        else:
            logger.info(
                "completion summary uses deterministic fallback because CE is not frozen | code=%s",
                migration_error,
            )
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


async def _record_project_completion_memory(
    state: TestPlanGraphState,
    *,
    ctx: RuntimeContext,
    summary_text: str,
    artifact: dict[str, Any],
) -> None:
    """Deterministic, idempotent Project progress memory; always fail-open."""
    project_context = state.get("project_context")
    if not isinstance(project_context, dict):
        project_context = getattr(ctx, "project_context", None)
    if not isinstance(project_context, dict) or not project_context.get("project_id"):
        return
    try:
        from app.services.project_memory_service import ProjectMemoryService

        async with ctx.session_factory() as session:
            await ProjectMemoryService(session).record_task_completion(
                user_id=ctx.user_internal_id,
                project_public_id=str(project_context["project_id"]),
                task_public_id=str(state.get("task_id") or ctx.task_internal_id),
                summary=summary_text,
                artifact=artifact,
                confirmed_sections=list(state.get("confirmed_sections") or []),
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — memory must never change task outcome
        logger.warning(
            "Project completion memory degraded | task=%s | err=%s",
            state.get("task_id"),
            type(exc).__name__,
        )


async def finalize_task_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """TASK_COMPLETED;若带 artifact + summary → emit 收尾。

    优先使用 task_summary_narrative_node 已通过叙事/事实校验的 LLM 正文；仅在
    该节点超时、契约校验失败或 Context Engine 不可用而未写入正文时，才回退
    fallback_summary_text。summary_facts 始终保留完整的结构化数据
    (章节数/业务模块数/审查统计/Artifact 状态)。

    payload 契约:
      * summary — 用户可读的完成说明(优先 LLM，失败才用确定性模板)
      * summary_facts — 结构化摘要事实(供前端摘要卡片)
      * artifact — 导出产物信息(供前端下载卡片)
      * format_check — 格式检查结果
      * checked_artifact_public_id — 格式检查绑定的 artifact ID
    """
    completed = mark_completed(state, NODE_FINALIZE_TASK)
    artifact = state.get("artifact") or {}
    canonical_facts = build_summary_facts(dict(state) if state else {})
    summary_text = str(state.get("summary") or "").strip()
    if not summary_text:
        summary_text = fallback_summary_text(dict(state) if state else {})

    format_check = state.get("format_check_result") or {}

    await _record_project_completion_memory(
        state,
        ctx=ctx,
        summary_text=summary_text,
        artifact=artifact if isinstance(artifact, dict) else {},
    )

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
        content=summary_text,
        payload={
            "summary": summary_text,
            "summary_facts": canonical_facts,
            "artifact": artifact,
            "format_check": format_check,
            "checked_artifact_public_id": state.get("checked_artifact_public_id"),
        },
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
