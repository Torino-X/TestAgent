"""v2 review-regen + format-check 节点 (Legacy F025 + F025-ext 段)。

对应业务段:TestPlanGeneratorTool 出章节 → 内容审查 + 修复 → 导出 → 导出完整性 +
格式损失检测。这一段是 Phase 2.4 / 2.9A.10 / 2.9A.18 修复最密集的一段。

节点(按执行顺序):
* ``review_result``         — ResultReviewTool 审 schema / 内容,
                              决定走 prepare_export / regenerate / repair_subgraph
* ``regenerate_sections``   — TestPlanRegenTool 对 block_issues 局部重写
* ``repair_subgraph``       — Phase 2.4 Repair Agent 子图入口
* ``repair_fallback``       — Phase 2.4 Repair Agent 失败/不可用时复用 regen 路径
* ``check_docx_format``     — DocxFormatCheckTool 检测 .docx secondary 结构
                              丢失,挂 format_loss_interrupt 让用户决策

条件边在 ``routing.py`` / ``routing_after_interrupt.py`` 给出,本文件只写节点函数。
所有节点遵循 ``async def node_xxx(state, *, ctx) -> dict`` 业务签名,通过
``_bind_async`` 包装后被 graph.add_node 注册。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from app.agent.enums import AgentEventType, TaskStatus

from app.agent_runtime.runtime_context import RuntimeContext

from app.agent_runtime.graphs.test_plan.constants import MAX_REVIEW_LOOPS
from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState
from app.agent_runtime._shared.artifact_contract import (
    is_artifact_present,
    normalize_export_artifact,
)
from .nodes_util import mark_completed
from .nodes_pre_confirm import _build_pending_narrative
from .routing import (
    NODE_FAIL_TASK,
    is_recoverable_repair_fallback,
    route_after_format_check,
    route_after_result_review,
)

logger = logging.getLogger(__name__)


NODE_REVIEW_STEP = "review_step"
NODE_REGENERATE_SECTIONS_STEP = "regenerate_sections_step"
NODE_CHECK_FORMAT_STEP = "check_docx_format_step"

# Phase 2.4 — Repair subgraph entry/exit node names
NODE_REPAIR_SUBGRAPH = "repair_subgraph_step"
NODE_REPAIR_FALLBACK = "repair_fallback_step"


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


def _extract_review_section_length(value: Any) -> int:
    """Phase 2.9A.11:Review 节点提取章节正文长度,支持 str/list/dict 三种类型。

    镜像 nodes_post_confirm._extract_section_content_length —
    Graph State 结构由 generate_test_plan_node 的 normalize_* 决定。
    """
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.strip())
    if isinstance(value, list):
        return sum(_extract_review_section_length(item) for item in value)
    if isinstance(value, dict):
        total = 0
        for v in value.values():
            if isinstance(v, str):
                total += len(v.strip())
            elif isinstance(v, (list, dict)):
                total += _extract_review_section_length(v)
        return total
    return 0


_SECTION_BINDING_KEYS = (
    "section_id",
    "title",
    "clean_title",
    "level",
    "order",
    "path",
    "paragraph_index",
    "body_start_index",
    "body_end_index",
    "table_indexes",
    "table_schemas",
    "source",
    "target_kind",
)


def _generation_config(template_structure: Any) -> Dict[str, Any]:
    if not isinstance(template_structure, dict):
        return {}
    config = template_structure.get("generation_config") or {}
    return config if isinstance(config, dict) else {}


def _section_aliases(section: Dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    for key in ("field", "section_id", "id", "title", "clean_title"):
        value = section.get(key)
        if value is not None and str(value):
            aliases.add(str(value))
    return aliases


def _find_ai_field_binding(template_structure: Any, section_key: str) -> Dict[str, Any]:
    config = _generation_config(template_structure)
    ai_fields = config.get("ai_fields") or []
    if not isinstance(ai_fields, list):
        return {}
    wanted = str(section_key or "")
    if not wanted:
        return {}
    for entry in ai_fields:
        if not isinstance(entry, dict):
            continue
        aliases = _section_aliases(entry)
        if wanted in aliases:
            return dict(entry)
    return {}


def _placeholder_from_binding(section_key: str, binding: Dict[str, Any]) -> Dict[str, Any]:
    field = str(binding.get("field") or section_key)
    section_id = str(binding.get("section_id") or section_key)
    placeholder: Dict[str, Any] = {
        "field": field,
        "section_id": section_id,
        "title": binding.get("title") or field or section_id,
        "content": "",
        "placeholder": True,
        "placeholder_reason": "missing_in_generation_result",
    }
    for key in _SECTION_BINDING_KEYS:
        if key in binding and key not in placeholder:
            placeholder[key] = binding.get(key)
    return placeholder


def _inject_missing_section_placeholders(
    test_plan_content: Dict[str, Any],
    schema_issues: List[Dict[str, Any]],
    template_structure: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """为宽容解析 / 截断场景下的缺失章节注入 placeholder 条目。

    Phase 2.9A.X：TestPlanGeneratorTool 整体重试被移除（F023）后，
    截断 JSON 通过 ``parse_json_lenient`` 拿到 partial payload + schema_issues。
    这里在 ResultReviewTool 跑 review 之前给缺失章节 push placeholder，
    让 ``_check_missing_section`` 规则能命中、RepairAgent 能调用
    ``TestPlanRegenTool(section_ids=[missing_ids])`` 定点补生成。

    Args:
        test_plan_content: 当前 state.test_plan_content（含 section_package）
        schema_issues: ``[{kind: missing_field, field: section_X}, ...]``

    Returns:
        新的 test_plan_content dict（不可变副本），section_package.generated_sections
        增加 placeholder 条目（不重复添加已有 section_id）
    """
    if not isinstance(test_plan_content, dict):
        test_plan_content = {}
    if not isinstance(schema_issues, list) or not schema_issues:
        return test_plan_content

    # 收集需要 placehold 的 section_id
    missing_ids: List[str] = []
    for iss in schema_issues:
        if not isinstance(iss, dict):
            continue
        kind = iss.get("kind")
        if kind not in ("missing_field", "missing_section"):
            continue
        field = iss.get("field")
        if field and field not in missing_ids:
            missing_ids.append(str(field))

    if not missing_ids:
        return test_plan_content

    # 在 section_package.generated_sections 加 placeholder（不覆盖已有）。
    # placeholder 必须带上与 ResultParser.build_section_package 同源的模板
    # 绑定元数据；否则 RepairAgent 后续只会补正文，WordExporter 仍无法根据
    # body_start_index 定位模板位置。
    new_content = dict(test_plan_content)
    section_pkg = new_content.get("section_package")
    if not isinstance(section_pkg, dict):
        section_pkg = {
            "generated_sections": [],
            "keep_sections": [],
            "manual_sections": [],
        }
    else:
        section_pkg = dict(section_pkg)
        section_pkg["generated_sections"] = list(section_pkg.get("generated_sections") or [])

    existing_ids = {
        alias
        for sec in section_pkg["generated_sections"]
        if isinstance(sec, dict)
        for alias in _section_aliases(sec)
    }
    for sid in missing_ids:
        binding = _find_ai_field_binding(template_structure, sid)
        placeholder = _placeholder_from_binding(sid, binding)
        placeholder_aliases = _section_aliases(placeholder)
        if placeholder_aliases & existing_ids:
            continue
        section_pkg["generated_sections"].append(placeholder)
        existing_ids.update(placeholder_aliases)

    new_content["section_package"] = section_pkg
    # 规范化结构中顶层 generated_sections 是 list，不是计数字段。
    # 保持它与 section_package.generated_sections 同步，避免下游按 list
    # 读取时被旧的 count 语义污染。
    if isinstance(new_content.get("generated_sections"), list):
        new_content["generated_sections"] = list(section_pkg["generated_sections"])
    return new_content


def _subset_generation_config(
    gen_config: Any,
    section_ids: list,
) -> Dict[str, Any]:
    """Phase 2.4 — ADR-2.4-3 bug-fix #2: 派生 generation_config_subset.

    Mirror of legacy ``app/agent/orchestrator.py`` ``_subset_generation_config`` —
    若 ``gen_config.sections`` 存在,只保留目标 sections;否则整段透传。

    Phase 2.9A.X bug fix：同时过滤 ``ai_fields[*]``（按 ``field`` 或
    ``section_id`` 命中 target section_ids）。原因：
    TestPlanRegenTool._find_config_entry 走 ``gen_config_subset.ai_fields``
    路径，老版本只过滤 ``sections`` 不过滤 ``ai_fields``，导致 ai_fields
    全量传入但 sections 子集，引起下游 LLM 收到不匹配的 schema hint。
    """
    if not isinstance(gen_config, dict):
        return {}
    keep = set(section_ids or [])

    subset: Dict[str, Any] = dict(gen_config)

    full_sections = gen_config.get("sections")
    if isinstance(full_sections, list):
        subset["sections"] = [
            s for s in full_sections
            if isinstance(s, dict) and (
                not keep
                or s.get("section_id") in keep
                or s.get("id") in keep
                or s.get("title") in keep
            )
        ]

    full_ai_fields = gen_config.get("ai_fields")
    if isinstance(full_ai_fields, list) and keep:
        subset["ai_fields"] = [
            entry for entry in full_ai_fields
            if isinstance(entry, dict) and (
                entry.get("field") in keep
                or entry.get("section_id") in keep
            )
        ]

    return subset


# ── review_result ────────────────────────────────────────────────────────────


async def review_result_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """调 ResultReviewTool;产出 review_result + level(+/ warning / failed)。

    review_loop_count 自增;route_after_review 看是否继续 regenerate。

    Phase 2.4 — ADR-2.4-3 bug-fix #1 + ADR-2.4-12: review_result 同时写
    ``review_issues`` (新,标准化格式) + ``block_issues`` (旧, 兼容 Phase 2.1 测试)。

    Phase 2.9A.10: 启动前先校验 ``test_plan_content`` 存在且可读 —
    不依赖 RuntimeContext._intermediate_state(后者在 Interrupt/Resume 后
    会丢失),只读 Graph State(由 LangGraph checkpointer 恢复)。
    缺失或空 → 直接 ``REVIEW_CONTENT_MISSING``,不调 ResultReviewTool,
    Fail-Fast 路由到 fail_task。
    """
    completed = mark_completed(state, NODE_REVIEW_STEP)
    adapter = _adapter(ctx)

    # ── Phase 2.9A.10/11: Review 边界校验 — content_source=graph_state
    # Phase 2.9A.11:Graph State 写入的是 normalize_test_plan_generation_output
    # 的结果(6 字段结构 + 统计字段),与 envelope.data 原始 dict 不同。
    # 这里必须用同一结构 — 兼容逻辑只在 normalize_* 集中处理。
    test_plan_content = state.get("test_plan_content") or {}

    # Phase 2.9A.11:优先读规范化字段;若 state 是 envelope.data(旧结构)
    # 则用 section_package.generated_sections 作为兜底。
    generated_sections = (
        test_plan_content.get("generated_sections")
        if isinstance(test_plan_content, dict) else None
    )
    section_package = (
        test_plan_content.get("section_package")
        if isinstance(test_plan_content, dict) else None
    )

    # 兼容旧 envelope.data 形态(state.test_plan_content == data)
    if not isinstance(generated_sections, list) and isinstance(section_package, dict):
        generated_sections = section_package.get("generated_sections")
    if not isinstance(generated_sections, list):
        generated_sections = None

    chapter_count = 0
    content_length = 0
    business_module_count = 0
    if isinstance(generated_sections, list):
        for sec in generated_sections:
            if not isinstance(sec, dict):
                continue
            chapter_count += 1
            # Phase 2.9A.11:支持 str / list[dict] / dict 三种 content 类型
            content_length += _extract_review_section_length(sec.get("content"))
            # business_module_count 取自 requirement_analysis
            req = state.get("requirement_analysis") or {}
            if isinstance(req, dict):
                modules = req.get("modules") or []
                if isinstance(modules, list):
                    business_module_count = len(modules)

    raw_schema_issues = (
        test_plan_content.get("schema_issues")
        if isinstance(test_plan_content, dict) else None
    )
    has_reviewable_schema_issues = (
        isinstance(raw_schema_issues, list)
        and any(isinstance(issue, dict) for issue in raw_schema_issues)
    )

    review_input_present = (
        isinstance(test_plan_content, dict)
        and bool(test_plan_content)
        and isinstance(section_package, dict)
        and bool(section_package)
        and (chapter_count > 0 or has_reviewable_schema_issues)
    )

    logger.info(
        "Phase 2.9A.10 review_result: pre-check | "
        "task_internal_id=%s | test_plan_content_present=%s | "
        "test_plan_content_type=%s | test_plan_content_length=%s | "
        "chapter_count=%s | business_module_count=%s | content_source=graph_state",
        ctx.task_internal_id,
        review_input_present,
        type(test_plan_content).__name__,
        content_length,
        chapter_count,
        business_module_count,
    )

    if not review_input_present:
        missing_fields: list[str] = []
        if not isinstance(test_plan_content, dict) or not test_plan_content:
            missing_fields.append("test_plan_content")
        if not isinstance(section_package, dict) or not section_package:
            missing_fields.append("test_plan_content.section_package")
        if chapter_count == 0 and not has_reviewable_schema_issues:
            missing_fields.append("test_plan_content.section_package.generated_sections")

        logger.error(
            "Phase 2.9A.10 review_result: REVIEW_CONTENT_MISSING | "
            "task_internal_id=%s | missing_fields=%s",
            ctx.task_internal_id,
            missing_fields,
        )
        await ctx.event_sink.emit(
            task_id=str(ctx.task_internal_id),
            graph_run_id=f"run-{ctx.task_internal_id}",
            node_name="ResultReviewTool",  # Phase 2.9A.11:节点名用真实工具名
            event_type=AgentEventType.STAGE_FAILED.value,
            title="审查阶段缺失测试方案内容",
            content="",
            payload={
                "code": "REVIEW_CONTENT_MISSING",
                "tool_name": "ResultReviewTool",  # Phase 2.9A.11:前端工具身份
                "tool_call_id": f"ResultReviewTool-{ctx.task_internal_id}",
                "failed_stage": "review_result",
                "missing_fields": missing_fields,
                "content_source": "graph_state",
            },
        )
        return {
            "last_error": {
                "code": "REVIEW_CONTENT_MISSING",
                "message": (
                    f"Review 阶段 Graph State 缺失测试方案内容: {missing_fields};"
                    " 不调用 ResultReviewTool,Fail-Fast 走 fail_task"
                ),
            },
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_REVIEW_STEP,
            "completed_nodes": completed,
            "next_node": NODE_FAIL_TASK,
        }

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
        patch = {
            "review_result": {"level": "passed", "issues": [], "block_issues": []},
            "review_loop_count": new_loops,
            "current_node": NODE_REVIEW_STEP,
            "completed_nodes": completed,
        }
        patch["next_node"] = route_after_result_review({**dict(state), **patch})
        return patch

    # ── Phase 2.9A.X: placeholder creation before review ──────────
    patched_test_plan_content: Dict[str, Any] | None = None
    schema_issues = test_plan_content.get("schema_issues") or []
    if isinstance(schema_issues, list) and schema_issues:
        new_content = _inject_missing_section_placeholders(
            test_plan_content,
            schema_issues,
            state.get("template_structure") or getattr(ctx, "template_structure", None),
        )
        if new_content is not test_plan_content:
            # 同步 state.test_plan_content + 局部变量，让下游 review 看到 placeholder
            state["test_plan_content"] = new_content
            test_plan_content = new_content
            patched_test_plan_content = new_content
            logger.info(
                "review_result_node: injected %d missing section placeholders | "
                "task_id=%s",
                len(schema_issues),
                ctx.task_internal_id,
            )

    envelope = await adapter.execute(
        tool_name="ResultReviewTool",
        inputs={
            "test_plan_content": test_plan_content,
            "review_standard": state.get("review_standard") or {},
            "previous_review": state.get("review_result") or {},
        },
        ctx_runtime=ctx,
        graph_state=dict(state) if state else None,
    )

    # ── Phase 2.9A.10: Review 工具执行失败 → Fail-Fast
    # envelope.success=False → 工具级失败(不是 review level=warning/failed);
    # 区分语义:review level 由数据决定(可修复),工具执行失败由工具决定(不可修复)。
    if not envelope.get("success"):
        error_code = (
            (envelope.get("error") or {}).get("code")
            if isinstance(envelope.get("error"), dict)
            else None
        ) or "REVIEW_TOOL_FAILED"
        logger.error(
            "Phase 2.9A.10 review_result: tool execution failed | "
            "task_internal_id=%s | error_code=%s",
            ctx.task_internal_id,
            error_code,
        )
        await ctx.event_sink.emit(
            task_id=str(ctx.task_internal_id),
            graph_run_id=f"run-{ctx.task_internal_id}",
            node_name="ResultReviewTool",  # Phase 2.9A.11
            event_type=AgentEventType.STAGE_FAILED.value,
            title="结果审查工具执行失败",
            content="",
            payload={
                "code": error_code,
                "tool_name": "ResultReviewTool",  # Phase 2.9A.11
                "tool_call_id": f"ResultReviewTool-{ctx.task_internal_id}",
                "failed_stage": "review_result",
                "error": envelope.get("error"),
            },
        )
        failed_patch = {
            "last_error": envelope.get("error") or {"code": error_code},
            "task_status": TaskStatus.FAILED.value,
            "current_phase": "failed",
            "current_node": NODE_REVIEW_STEP,
            "completed_nodes": completed,
        }
        if patched_test_plan_content is not None:
            failed_patch["test_plan_content"] = patched_test_plan_content
        return _with_pending_narrative(
            failed_patch,
            state=state,
            tool_name="ResultReviewTool",
            tool_call_id=str(envelope.get("tool_call_id") or ""),
            attempt=int(envelope.get("attempt") or 1),
            terminal_status="failed",
            continuation_route=NODE_FAIL_TASK,
            duration_ms=envelope.get("duration_ms"),
            source_event_id=_terminal_event_id(adapter),
        )

    data = envelope.get("data") or {}
    patch = {
        "review_result": data if isinstance(data, dict) else {"level": "failed", "issues": []},
        "review_loop_count": new_loops,
        "current_node": NODE_REVIEW_STEP,
        "completed_nodes": completed,
    }
    if patched_test_plan_content is not None:
        patch["test_plan_content"] = patched_test_plan_content
    continuation_route = route_after_result_review({**dict(state), **patch})
    return _with_pending_narrative(
        patch,
        state=state,
        tool_name="ResultReviewTool",
        tool_call_id=str(envelope.get("tool_call_id") or ""),
        attempt=int(envelope.get("attempt") or 1),
        terminal_status="success",
        continuation_route=continuation_route,
        duration_ms=envelope.get("duration_ms"),
        source_event_id=_terminal_event_id(adapter),
    )


# ── regenerate_sections ─────────────────────────────────────────────────────


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
        graph_state=dict(state) if state else None,
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


async def _try_recover_artifact_from_repository(
    ctx: RuntimeContext,
    state: TestPlanGraphState,
) -> Dict[str, Any]:
    """Phase 2.9A.17 §5.7: 受控 Repository 恢复(异步)。

    当 Graph State.artifact 不可用时,按 ``task_internal_id + user_internal_id +
    artifact_type=test_plan_word`` 拉当前任务最近一次成功写入的 Artifact 行,
    校验文件磁盘存在后回传 dict,供 docx format_check 复用。

    强约束 (5.7):
      * 只看当前 task + user;不跨任务、不跨用户、不按文件名搜索、不遍历目录;
      * 不选择"当前用户最近 Artifact" — 必须 task 内最新一条;
      * 该函数只补 artifact 字段,不擅自执行 export;补回后 DocxFormatCheckTool
        走正常路径。
    """
    session = getattr(ctx, "session", None)
    if session is None:
        return {}
    from sqlalchemy import select
    from app.models.artifact import Artifact

    try:
        stmt = (
            select(Artifact)
            .where(
                Artifact.user_id == ctx.user_internal_id,
                Artifact.task_id == ctx.task_internal_id,
                Artifact.artifact_type == "test_plan_word",
                Artifact.deleted_at.is_(None),
            )
            .order_by(Artifact.created_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - 恢复层永远不能 crash format_check
        logger.warning(
            "Phase 2.9A.17 _try_recover_artifact_from_repository: err=%s", exc,
        )
        return {}

    if row is None:
        return {}

    # Phase 2.9A.17: 物理文件必须存在 — 否则视为恢复失败
    storage_path = row.storage_path or ""
    if not storage_path:
        return {}
    if row.storage_type == "oss":
        from app.storage.oss_storage import object_storage

        if not await object_storage.exists(storage_path):
            return {}
    else:
        # Legacy rows from before the OSS migration may still point at local disk.
        from app.storage.local_storage import local_storage

        abs_path = (
            local_storage._base / storage_path
            if not os.path.isabs(storage_path)
            else storage_path
        )
        if not abs_path.exists():
            logger.warning(
                "Phase 2.9A.17 _try_recover_artifact_from_repository: "
                "physical file missing | task=%s | public_id=%s",
                ctx.task_internal_id, row.public_id,
            )
            return {}

    return {
        "public_id": row.public_id,
        "artifact_id": row.public_id,
        "artifact_type": row.artifact_type,
        "file_name": row.file_name,
        "file_ext": row.file_ext,
        "mime_type": row.mime_type,
        "file_size": row.file_size,
        "storage_type": row.storage_type,
        "storage_path": row.storage_path,
        "version_no": row.version_no,
    }





async def check_docx_format_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """调 DocxFormatCheckTool;产出 level(passed/warning/failed/loss_detected)与 losses。

    Phase 2.9A.16: 传入 ``artifact`` + ``artifact_path``,DocxFormatCheckTool 通过
    ``inputs.artifact_path`` 走到 ``_absolutise(storage_path)`` 直接打开文件。
    Phase 2.9A.17:
      * 状态先验证(``storage_path`` + ``public_id``)否则进入受控 Repository 恢复;
      * 4 个诊断日志字段(state_artifact_present / state_artifact_public_id /
        state_storage_path_present / inputs_artifact_path_present);
      * 失败错误码分层(交给 DocxFormatCheckTool 的 6 错误码体系);
      * 写回 ``checked_artifact_public_id`` 让 routing / GraphState 与 artifact
        解耦时仍可定位校验目标;
      * format_loop_count 自增;命中 ``MAX_FORMAT_LOOPS`` 时允许走
        ``pause_for_legacy_format_decision``(与 routing 一致)。
    """
    completed = mark_completed(state, NODE_CHECK_FORMAT_STEP)
    adapter = _adapter(ctx)
    loops = int(state.get("format_loop_count") or 0)

    # Phase 2.9A.17 §5.5: 4 个诊断字段
    raw_artifact = state.get("artifact") or {}
    state_artifact_present = is_artifact_present(raw_artifact) if isinstance(raw_artifact, dict) else False
    state_artifact_public_id = (
        raw_artifact.get("public_id") if isinstance(raw_artifact, dict) else None
    )
    state_storage_path_present = bool(
        isinstance(raw_artifact, dict) and raw_artifact.get("storage_path")
    )

    if not state_artifact_present:
        # Phase 2.9A.17 §5.7: 先尝试受控 Repository 恢复(默认 task+user 边界)
        recovered = await _try_recover_artifact_from_repository(ctx, state)
        if is_artifact_present(recovered):
            normalized = normalize_export_artifact(recovered)
            if normalized.ok:
                logger.warning(
                    "Phase 2.9A.17 check_docx_format: artifact_state_recovered | "
                    "task_internal_id=%s | recovered_public_id=%s",
                    ctx.task_internal_id,
                    normalized.artifact.get("public_id"),
                )
                raw_artifact = normalized.artifact
                state_artifact_present = True
                state_storage_path_present = True
                state_artifact_public_id = raw_artifact.get("public_id")
            else:
                logger.warning(
                    "Phase 2.9A.17 check_docx_format: recovered 失败 | "
                    "task_internal_id=%s | errors=%s",
                    ctx.task_internal_id,
                    normalized.errors,
                )
        else:
            logger.error(
                "Phase 2.9A.17 check_docx_format: artifact_state_invalid_no_recovery | "
                "task_internal_id=%s | state_artifact_present=%s",
                ctx.task_internal_id, state_artifact_present,
            )

    artifact_storage_path = (
        raw_artifact.get("storage_path") if isinstance(raw_artifact, dict) else None
    )
    inputs_artifact_path_present = bool(artifact_storage_path)

    # Phase 2.9A.17: 诊断日志(绝不打印绝对路径)
    logger.info(
        "Phase 2.9A.17 check_docx_format: diagnostic | "
        "task_internal_id=%s | state_artifact_present=%s | "
        "state_artifact_public_id=%s | state_storage_path_present=%s | "
        "inputs_artifact_path_present=%s | format_loop_count=%s",
        ctx.task_internal_id,
        state_artifact_present,
        state_artifact_public_id,
        state_storage_path_present,
        inputs_artifact_path_present,
        loops,
    )

    if adapter is None:
        patch = {
            "format_check_result": {"level": "passed", "losses": []},
            "format_loop_count": loops + 1,
            "pending_format_losses": [],
            "current_node": NODE_CHECK_FORMAT_STEP,
            "completed_nodes": completed,
        }
        patch["next_node"] = route_after_format_check({**dict(state), **patch})
        return patch

    execution_inputs = {
        "artifact": raw_artifact,
        "artifact_path": artifact_storage_path,
        "format_loss_confirmation": state.get("format_loss_confirmation") or {},
    }
    if raw_artifact.get("storage_type") == "oss" and artifact_storage_path:
        from app.storage.oss_storage import object_storage

        async with object_storage.stage_file(artifact_storage_path, suffix=".docx") as staged:
            execution_inputs["artifact_path"] = str(staged)
            envelope = await adapter.execute(
                tool_name="DocxFormatCheckTool", inputs=execution_inputs,
                ctx_runtime=ctx, graph_state=dict(state) if state else None,
            )
    else:
        envelope = await adapter.execute(
            tool_name="DocxFormatCheckTool", inputs=execution_inputs,
            ctx_runtime=ctx, graph_state=dict(state) if state else None,
        )

    data = envelope.get("data") or {}
    # OSS artefacts are read via a temporary staged file; do not leak the
    # temp path back to consumers — it is deleted as soon as ``stage_file``
    # exits.  ``checked_artifact_public_id`` is the durable identifier.
    if raw_artifact.get("storage_type") == "oss" and "artifact_path" in data:
        data.pop("artifact_path", None)
    losses = data.get("losses") or []
    raw_level = data.get("level") if isinstance(data, dict) else "passed"

    # Phase 2.9A.18:字面值统一为 ``passed / warning / blocked / failed``。
    # DocxFormatCheckTool 仍返回 ``level``,这里既保留 level(向后兼容),
    # 也写 ``status`` 字段给 Phase 2.9A.18 路由识别。
    status_alias = "blocked" if raw_level == "loss_detected" else raw_level

    canonical_result = (
        {**data, "status": status_alias, "level": raw_level}
        if isinstance(data, dict)
        else {"status": "passed", "level": "passed", "losses": []}
    )

    # Phase 2.9A.17 §5.6: 6 个错误码语义(交给 Tool 内部,这里只读 + 路由)
    error_code = None
    if not envelope.get("success"):
        err_obj = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        error_code = err_obj.get("code")

    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_CHECK_FORMAT_STEP,
        event_type=AgentEventType.DOCX_FORMAT_CHECKED.value,
        title="格式自检",
        content="",
        payload={
            "status": status_alias,
            "level": raw_level,
            "losses": losses if isinstance(losses, list) else [],
            "loop_count": loops,
            "checked_artifact_public_id": state_artifact_public_id,
            "error_code": error_code,
        },
    )

    # Phase 2.9A.X: format_loss 入口 B 兜底。DocxFormatCheckTool 检测到
    # ``loss_detected`` 时,barrier_path_map 把 ``pause_for_legacy_format_decision``
    # 重定向到 ``format_loss_interrupt_node``(interrupt_enabled=True 模式下,
    # 二者别名均为 ``format_loss_interrupt``,见 graph.py:171)。但
    # ``format_loss_interrupt_node``(nodes_interrupts.py:208)在 interrupt()
    # 之前**不发** FORMAT_LOSS_CONFIRM_REQUESTED 事件,而真正发事件的
    # ``pause_for_legacy_format_decision_node``(nodes_post_confirm.py:876)
    # 在该模式下不会被走到,导致前端收不到"需要确认"提示,任务静默挂起。
    #
    # 与入口 A (export_word_node:810) 对齐:在 loss_detected 分支补一次
    # FORMAT_LOSS_CONFIRM_REQUESTED emit,使前端能在
    # ``format_loss_interrupt_node`` 挂起前收到确认请求。
    if raw_level == "loss_detected" and isinstance(losses, list) and losses:
        try:
            await ctx.event_sink.emit(
                task_id=str(ctx.task_internal_id),
                graph_run_id=f"run-{ctx.task_internal_id}",
                node_name=NODE_CHECK_FORMAT_STEP,
                event_type=AgentEventType.FORMAT_LOSS_CONFIRM_REQUESTED.value,
                title="格式丢失需要确认",
                content="",
                payload={"losses": list(losses)},
            )
        except Exception:
            logger.warning(
                "check_docx_format_node: emit format_loss_review event failed | "
                "task_internal_id=%s",
                ctx.task_internal_id,
                exc_info=True,
            )

    patch = {
        "format_check_result": canonical_result,
        "format_loop_count": loops + 1,
        "pending_format_losses": losses if isinstance(losses, list) else [],
        # Phase 2.9A.17: 持续绑定 checked_artifact_public_id,
        # 让后续节点(routing_after_interrupt / finalize_task)以该字段定位校验目标
        "checked_artifact_public_id": state_artifact_public_id,
        "current_node": NODE_CHECK_FORMAT_STEP,
        "completed_nodes": completed,
    }
    continuation_route = route_after_format_check({**dict(state), **patch})
    return _with_pending_narrative(
        patch,
        state=state,
        tool_name="DocxFormatCheckTool",
        tool_call_id=str(envelope.get("tool_call_id") or ""),
        attempt=int(envelope.get("attempt") or 1),
        terminal_status=("success" if envelope.get("success", True) else "failed"),
        continuation_route=continuation_route,
        duration_ms=envelope.get("duration_ms"),
        source_event_id=_terminal_event_id(adapter),
    )


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

    # CE-04 FINAL REVISION §三：MIG_REVIEW flag 决定 Review 入口路由。
    #   MIG_REVIEW=true  → 把 ContextInvokerBridge（先 bind）作为 llm_client
    #                      传入 Repair 子图，Review→Repair 全链路经 Invoker；
    #                      不静默回退 legacy。
    #   MIG_REVIEW=false → 保持 legacy（ctx.llm_client）。
    from app.context_engine.feature_flags import is_agent_context_migration_enabled

    # CE-05 WP-2：任务路径经 ctx.task_flag_resolver 读 MIG_REVIEW（冻结 Manifest）；
    # resolver 缺失/损坏 → fail closed，不读取进程级 MIG 配置。
    resolver = getattr(ctx, "task_flag_resolver", None)
    mig_review = is_agent_context_migration_enabled(resolver, "MIG_REVIEW")
    if mig_review:
        bridge = getattr(ctx, "context_llm_invoker", None)
        if bridge is None or not getattr(bridge, "available", False):
            logger.warning(
                "repair_subgraph MIG_REVIEW=true 但 Invoker 不可用 | task_id=%s",
                ctx.task_internal_id,
            )
            return {
                "last_error": {
                    "code": "MIGRATION_INVOKER_UNAVAILABLE",
                    "message": "MIG_REVIEW=true 但 Invoker 未构建，Review 走失败策略",
                },
                "task_status": "failed",
                "current_node": NODE_REPAIR_SUBGRAPH,
                "completed_nodes": completed,
                "repair_loop_count": new_repair_loops,
            }
        llm_client = bridge.bind(
            user_id=ctx.user_internal_id,
            call_site="test_plan.review",
            task_id=ctx.task_internal_id,
            runtime_context=ctx,
        )
    else:
        return {
            "last_error": {
                "code": "MIGRATION_CONTEXT_REQUIRED",
                "message": "测试方案审查/修复仅支持 Context Engine；当前任务未冻结 MIG_REVIEW，已阻止旧 LLM 回退。",
            },
            "task_status": "failed",
            "current_node": NODE_REPAIR_SUBGRAPH,
            "completed_nodes": completed,
            "repair_loop_count": new_repair_loops,
        }

    if adapter is None or llm_client is None:
        # Fallback: 字节级复用 regenerate_sections_node 主体。
        # 记录结构化缺失原因,便于区分"配置关闭"与"依赖缺失"。
        missing = [
            name
            for name, val in (("tool_adapter", adapter), ("llm_client", llm_client))
            if val is None
        ]
        logger.warning(
            "RepairSubgraph unavailable | task_id=%s | "
            "missing_dependencies=%s | fallback=deterministic_regenerate",
            ctx.task_internal_id,
            missing,
        )
        result_state = await regenerate_sections_node(state, ctx=ctx)
        result_state["repair_fallback_reason"] = (
            f"missing_runtime_dependencies:{','.join(missing)}"
        )
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
    # Use the current state value because the repair subgraph may have
    # written a fresh re-review result. The pre-repair snapshot still
    # contains stale block issues and must not overwrite the clean pass.
    review_passed = bool(getattr(repair_result, "review_passed", False))
    updated_review = dict(state.get("review_result") or review)
    updated_review["level"] = "passed" if review_passed else "failed"
    updated_review["repair_issues_resolved"] = list(getattr(repair_result, "issues_resolved", []) or [])
    updated_review["repair_issues_remaining"] = list(getattr(repair_result, "issues_remaining", []) or [])
    updated_review["repair_unresolved_issue_details"] = list(
        getattr(repair_result, "remaining_issue_details", []) or []
    )
    if review_passed:
        updated_review["passed"] = True
        updated_review["review_issues"] = []
        updated_review["block_issues"] = []
        updated_review["issues"] = []
        updated_review["repair_issues_remaining"] = []
        updated_review["repair_unresolved_issue_details"] = []

    patch = {
        "review_result": updated_review,
        "repair_result": repair_result.model_dump() if hasattr(repair_result, "model_dump") else {},
        "repair_loop_count": new_repair_loops,
        "repair_fallback_reason": getattr(repair_result, "fallback_reason", None),
        "current_node": NODE_REPAIR_SUBGRAPH,
        "completed_nodes": completed,
    }
    if not review_passed:
        remaining = updated_review["repair_issues_remaining"]
        patch["last_error"] = {
            "code": "REPAIR_UNRESOLVED_BLOCK_ISSUES",
            "message": (
                f"自动修复后仍有 {len(remaining)} 个阻断问题，未执行 Word 导出；"
                "请修复模板或重试生成。"
            ),
            "remaining_issue_count": len(remaining),
        }
        # Let the graph router distinguish recoverable parser/context failures
        # from terminal operational failures. Marking every unresolved repair as
        # failed here used to bypass repair_fallback_node completely.
        if not is_recoverable_repair_fallback(patch["repair_fallback_reason"]):
            patch["task_status"] = TaskStatus.FAILED.value
            patch["current_phase"] = "failed"
    return patch


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
