"""Phase 2.9B.4 — Tool Context Builder Registry。

每个 Builder 只输出白名单事实(压缩的事实快照),绝不传入完整 Graph State /
完整文档 / 系统路径 / API Key / 异常堆栈。未知 Tool 使用严格 GenericBuilder。

设计约束:
* 不得在 NarrativeComposer 中写大型 if/elif —— 用注册表分发。
* 每个 Builder 可读取 Graph State 中对应的业务字段(requirement_analysis /
  template_structure / knowledge_search_result / section_suggestions /
  test_plan_content / review_result / artifact / format_check_result)。
* 输出数字必须同时写入 fact_constraints.allowed_numeric_facts(供事实校验)。
* 输出文件名必须写入 fact_constraints.allowed_file_names。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from .schemas import (
    NarrativeFactConstraints,
    ToolNarrativeContext,
    ToolTerminalStatus,
)


def _num(value: Any) -> Optional[int]:
    """安全转 int;非数字返回 None。

    排除 bool(因为 Python 里 bool 是 int 的子类,要先拒绝),
    float 仅在 .is_integer() 时接受(避免 0.5 → 0 这类静默失真)。
    """
    # bool 在 Python 里也是 int 子类(True/False),这里不能让它混进数字白名单
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _str(value: Any, limit: int = 120) -> str:
    # 把任意值安全转换为限定长度的字符串(便于序列化进 fact_constraints 白名单)。
    # 非字符串 → "";超过 limit 截断(防 LLM prompt 里被长字符串撑爆 token)。
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _first_str(*values: Any, limit: int = 120) -> str:
    # 在多个候选字段里取第一个非空串(常用在"文件名可能在 file_name / original_name / name 三处
    # 之一"的兼容场景)。
    for value in values:
        text = _str(value, limit)
        if text:
            return text
    return ""


def _add_numbers(constraints: NarrativeFactConstraints, values: list[Any]) -> None:
    # 把若干数值候选添加入数字白名单(去重 + 过滤 None)。
    # 白名单机制由 NarrativeValidator 强制执行,这里只负责收集。
    for v in values:
        n = _num(v)
        if n is not None and n not in constraints.allowed_numeric_facts:
            constraints.allowed_numeric_facts.append(n)


def _register_display_name(
    constraints: NarrativeFactConstraints,
    display_name: str,
    *,
    internal_id: str = "",
) -> str:
    """登记用户可见展示名 + 内部 public_id 到字面量白名单。

    Phase 2.9B.5:
    * ``display_name``(original_name / document_name / artifact_name /
      template_name)是模型应当使用的用户可见名字,写入
      ``allowed_literal_facts``(及其内嵌数字豁免)。
    * 内部 public_id(如 ``file_dc6d128c``)不是用户可见叙事素材,但模型
      一旦引用它,其内嵌数字(6/128)必须豁免,故写入
      ``numeric_exempt_literals``。
    """
    name = (display_name or "").strip()
    for alias in _display_name_aliases(name):
        if alias not in constraints.allowed_literal_facts:
            constraints.allowed_literal_facts.append(alias)
        if alias not in constraints.allowed_file_names:
            constraints.allowed_file_names.append(alias)
        if alias not in constraints.numeric_exempt_literals:
            constraints.numeric_exempt_literals.append(alias)
    internal = (internal_id or "").strip()
    if internal and internal not in constraints.numeric_exempt_literals:
        constraints.numeric_exempt_literals.append(internal)
    return name


def _display_name_aliases(display_name: str) -> list[str]:
    """Return user-visible aliases for stored upload/artifact names."""
    name = (display_name or "").strip()
    if not name:
        return []
    aliases: list[str] = []

    def add(value: str) -> None:
        value = (value or "").strip()
        if value and value not in aliases:
            aliases.append(value)

    add(name)
    basename = name.replace("\\", "/").rsplit("/", 1)[-1]
    add(basename)

    # Upload/artifact storage commonly prefixes names with either
    # YYYYMMDD_<hex>_ or YYYYMMDD_HHMMSS_. The model should be allowed to use
    # the original display name without that storage prefix.
    current = basename
    for pattern in (
        r"^\d{8}_[0-9a-fA-F]{8}_(.+)$",
        r"^\d{8}_\d{6}_(.+)$",
    ):
        match = re.match(pattern, current)
        if match:
            current = match.group(1)
            add(current)
    return aliases


class _BaseToolContextBuilder:
    """Base builder: 组装 ToolNarrativeContext + 收集约束。"""

    tool_name: str = ""

    def build(
        self,
        *,
        graph_state: Dict[str, Any],
        tool_call_id: str,
        source_event_id: str,
        attempt: int,
        terminal_status: ToolTerminalStatus,
        duration_ms: Optional[int],
        continuation_route: str,
    ) -> ToolNarrativeContext:
        constraints = NarrativeFactConstraints()
        facts = self._collect_facts(graph_state, constraints)
        _add_numbers(constraints, [duration_ms])
        return ToolNarrativeContext(
            task_id=_str(graph_state.get("task_id"), 64),
            graph_run_id=_str(graph_state.get("graph_run_id"), 64),
            goal="根据需求文档和模板生成测试方案",
            current_phase=_str(graph_state.get("current_phase"), 40),
            tool_name=self.tool_name,
            tool_call_id=_str(tool_call_id, 80),
            source_event_id=_str(source_event_id, 120),
            attempt=max(1, int(attempt or 1)),
            terminal_status=terminal_status,
            duration_ms=duration_ms,
            input_facts=facts.get("input", {}),
            output_facts=facts.get("output", {}),
            state_diff=facts.get("state_diff", {}),
            execution_context={
                "next_node": continuation_route,
                "retry_planned": terminal_status == "retry_scheduled",
                "waiting_for_user": bool(graph_state.get("pause_marker")),
            },
            fact_constraints=constraints,
        )

    def _collect_facts(
        self,
        graph_state: Dict[str, Any],
        constraints: NarrativeFactConstraints,
    ) -> Dict[str, Dict[str, Any]]:
        raise NotImplementedError


class RequirementParserContextBuilder(_BaseToolContextBuilder):
    tool_name = "RequirementParserTool"

    def _collect_facts(self, state, constraints):
        analysis = state.get("requirement_analysis") or {}
        structure = analysis.get("document_structure") or []
        tables = analysis.get("table_summaries") or []
        images = analysis.get("image_texts") or []
        doc_title = analysis.get("document_title") or ""
        section_count = len(structure)
        table_count = len(tables)
        image_count = len(images)
        _add_numbers(
            constraints,
            [section_count, table_count, image_count,
             _num(analysis.get("recognized_image_count"))],
        )
        # Phase 2.9B.5: 优先使用用户可见 original_name(document_name),
        # 只有缺省时才回退到内部 public_id。内部 public_id 登记为豁免字面量,
        # 不要求模型把它当作展示名写入 summary/details。
        internal_id = _str(state.get("requirement_file_id"))
        file_info = analysis.get("file_info") or {}
        if not isinstance(file_info, dict):
            file_info = {}
        display_name = _first_str(
            state.get("requirement_document_name"),
            state.get("requirement_original_name"),
            state.get("requirement_file_name"),
            state.get("uploaded_requirement_name"),
            analysis.get("document_name"),
            analysis.get("original_name"),
            analysis.get("file_name"),
            analysis.get("original_file_name"),
            analysis.get("source_file_name"),
            analysis.get("requirement_file_name"),
            analysis.get("document_filename"),
            analysis.get("original_filename"),
            file_info.get("document_name"),
            file_info.get("original_name"),
            file_info.get("file_name"),
            file_info.get("original_file_name"),
            file_info.get("source_file_name"),
            file_info.get("document_filename"),
            file_info.get("original_filename"),
            file_info.get("name"),
        )
        file_name = _register_display_name(
            constraints, display_name, internal_id=internal_id,
        ) or internal_id
        return {
            "input": {"file_name": file_name},
            "output": {
                "section_count": section_count,
                "table_count": table_count,
                "image_count": image_count,
                "recognized_image_count": _num(analysis.get("recognized_image_count")),
                "document_title": _str(doc_title, 40),
            },
            "state_diff": {
                "requirement_analysis_created": bool(analysis),
            },
        }


class TemplateParserContextBuilder(_BaseToolContextBuilder):
    tool_name = "TemplateParserTool"

    def _collect_facts(self, state, constraints):
        structure = state.get("template_structure") or {}
        sections = structure.get("sections") or []
        template_name = _str(structure.get("template_name"))
        table_count = 0
        for s in sections if isinstance(sections, list) else []:
            if isinstance(s, dict):
                table_count += _num(s.get("table_schemas")) or 0
        ai_count = 0
        keep_count = 0
        for s in sections if isinstance(sections, list) else []:
            if isinstance(s, dict):
                ai_count += _num(s.get("ai_fields")) or 0
                keep_count += _num(s.get("keep_sections")) or 0
        _add_numbers(
            constraints,
            [len(sections), table_count, ai_count, keep_count,
             _num(structure.get("sections_count")),
             _num(structure.get("total_sections"))],
        )
        _register_display_name(
            constraints, template_name,
            internal_id=_str(state.get("template_file_id")),
        )
        return {
            "input": {"file_name": template_name},
            "output": {
                "top_level_sections": _num(structure.get("sections_count")),
                "total_sections": len(sections),
                "table_count": table_count,
                "fixed_section_count": _num(structure.get("fixed_sections_count")),
                "generation_config_identified": bool(structure.get("generation_config")),
                "review_standard_identified": bool(structure.get("review_standard")),
            },
            "state_diff": {
                "template_structure_created": bool(structure),
            },
        }


class KnowledgeSearchContextBuilder(_BaseToolContextBuilder):
    tool_name = "KnowledgeSearchTool"

    def _collect_facts(self, state, constraints):
        kb = state.get("knowledge_search_result") or {}
        skip_reason = _str(kb.get("skip_reason"))
        hit_count = _num(kb.get("hit_count"))
        _add_numbers(constraints, [hit_count])
        enabled = not skip_reason
        return {
            "input": {"query": _str(kb.get("query"), 120)},
            "output": {
                "enabled": enabled,
                "skipped": bool(skip_reason),
                "skip_reason": skip_reason,
                "hit_count": hit_count,
                "used_entries": _num(kb.get("used_count")),
            },
            "state_diff": {
                "knowledge_search_attempted": bool(kb),
            },
        }


class SectionSuggestionContextBuilder(_BaseToolContextBuilder):
    tool_name = "SectionSuggestionTool"

    def _collect_facts(self, state, constraints):
        suggestions = state.get("section_suggestions") or {}
        sections = suggestions.get("sections") or []
        ai = keep = manual = skip = 0
        for s in sections if isinstance(sections, list) else []:
            if not isinstance(s, dict):
                continue
            action = s.get("action") or s.get("suggestedAction") or ""
            if action == "ai_generate":
                ai += 1
            elif action == "keep_template":
                keep += 1
            elif action == "manual_fill":
                manual += 1
            elif action == "skip":
                skip += 1
        _add_numbers(constraints, [len(sections), ai, keep, manual, skip])
        return {
            "input": {},
            "output": {
                "total_sections": len(sections),
                "ai_generate_count": ai,
                "keep_template_count": keep,
                "manual_fill_count": manual,
                "skip_count": skip,
                "needs_user_confirm": True,
            },
            "state_diff": {
                "section_suggestions_created": bool(suggestions),
            },
        }


class TestPlanGeneratorContextBuilder(_BaseToolContextBuilder):
    tool_name = "TestPlanGeneratorTool"

    def _collect_facts(self, state, constraints):
        content = state.get("test_plan_content") or {}
        sections = content.get("sections") or []
        attempt = _num(state.get("attempt", {}).get("TestPlanGeneratorTool")) or 1
        retry_planned = bool(state.get("last_retry_decision", {}).get("TestPlanGeneratorTool"))
        _add_numbers(constraints, [len(sections), attempt])
        return {
            "input": {"attempt": attempt},
            "output": {
                "generated_sections": len(sections),
                "current_attempt": attempt,
                "schema_valid": bool(content.get("schema_valid", True)),
                "retry_planned": retry_planned,
                "completed_modules": _num(content.get("completed_modules")),
            },
            "state_diff": {
                "test_plan_content_created": bool(content),
            },
        }


class ReviewContextBuilder(_BaseToolContextBuilder):
    tool_name = "ResultReviewTool"

    def _collect_facts(self, state, constraints):
        review = state.get("review_result") or {}
        block_count = _num(review.get("block_count"))
        warning_count = _num(review.get("warning_count"))
        suggestion_count = _num(review.get("suggestion_count"))
        passed = bool(review.get("passed", review.get("level") == "passed"))
        _add_numbers(constraints, [block_count, warning_count, suggestion_count])
        return {
            "input": {},
            "output": {
                "blocking_issues": block_count or 0,
                "warnings": warning_count or 0,
                "recommendations": suggestion_count or 0,
                "passed": passed,
                "entered_repair": bool(state.get("repair_agent_enabled")),
                "repair_rounds": _num(state.get("repair_loop_count")) or 0,
            },
            "state_diff": {
                "review_completed": bool(review),
            },
        }


class DocxExportContextBuilder(_BaseToolContextBuilder):
    tool_name = "WordExportTool"

    def _collect_facts(self, state, constraints):
        artifact = state.get("artifact") or {}
        name = _first_str(
            artifact.get("file_name"),
            artifact.get("name"),
            artifact.get("artifact_name"),
            artifact.get("original_name"),
            artifact.get("filename"),
        )
        size = _num(
            artifact.get("file_size")
            or artifact.get("size_bytes")
            or artifact.get("size")
            or artifact.get("bytes")
        )
        public_id = _str(artifact.get("public_id"))
        _add_numbers(constraints, [size])
        _register_display_name(constraints, name, internal_id=public_id)
        return {
            "input": {},
            "output": {
                "artifact_name": name,
                "artifact_type": _str(artifact.get("artifact_type") or "test_plan_word", 40),
                "file_size": size,
                "export_success": bool(artifact),
                "download_available": bool(public_id),
            },
            "state_diff": {
                "artifact_created": bool(artifact),
            },
        }


class DocxFormatCheckContextBuilder(_BaseToolContextBuilder):
    tool_name = "DocxFormatCheckTool"

    def _collect_facts(self, state, constraints):
        fmt = state.get("format_check_result") or {}
        losses = fmt.get("losses") or fmt.get("user_visible_losses") or []
        loss_count = len(losses) if isinstance(losses, list) else _num(fmt.get("loss_count")) or 0
        needs_confirm = bool(state.get("pending_format_losses"))
        _add_numbers(constraints, [loss_count])
        return {
            "input": {},
            "output": {
                "format_check_result": _str(fmt.get("status") or fmt.get("level") or "passed", 40),
                "loss_count": loss_count,
                "needs_user_confirm": needs_confirm,
                "deliverable_ready": not needs_confirm,
            },
            "state_diff": {
                "format_checked": bool(fmt),
            },
        }


class GenericToolContextBuilder(_BaseToolContextBuilder):
    """未知 Tool 的严格 Builder — 只允许 Tool 名/终态/耗时/脱敏摘要。

    用作兜底:新增 Tool 时如果忘记注册 builder,这里就硬出"工具完成" —
    不会因为没有 builder 就崩,但模型可写的事实很少,fallback 概率高。
    """

    def __init__(self, tool_name: str):
        self.tool_name = tool_name

    def _collect_facts(self, state, constraints):
        return {
            "input": {},
            "output": {
                "terminal_status_note": "Tool 执行完成。",
            },
            "state_diff": {},
        }


# ── Registry ──────────────────────────────────────────────────────────────


# 叙事注册表:每个 Tool 一个 Builder,负责从 Graph State 抽白名单事实。
# 添加新 Tool 时: 写一个 ContextBuilder 子类(继承 _BaseToolContextBuilder),
# 然后在这里加一行映射 ——
# 与 composer 无关,纯查表。
TOOL_NARRATIVE_CONTEXT_BUILDERS: dict[str, _BaseToolContextBuilder] = {
    "RequirementParserTool": RequirementParserContextBuilder(),
    "TemplateParserTool": TemplateParserContextBuilder(),
    "KnowledgeSearchTool": KnowledgeSearchContextBuilder(),
    "SectionSuggestionTool": SectionSuggestionContextBuilder(),
    "TestPlanGeneratorTool": TestPlanGeneratorContextBuilder(),
    "ResultReviewTool": ReviewContextBuilder(),
    "WordExportTool": DocxExportContextBuilder(),
    "DocxFormatCheckTool": DocxFormatCheckContextBuilder(),
}


def get_tool_context_builder(tool_name: str) -> _BaseToolContextBuilder:
    """按 tool_name 取 Builder;未知 Tool 返回严格 Generic Builder。

    narrative_barrier 节点在 trigger 阶段调用这里,获取到 builder 后
    调用 ``builder.build(...)`` 构造 ToolNarrativeContext。
    """
    builder = TOOL_NARRATIVE_CONTEXT_BUILDERS.get(tool_name)
    if builder is None:
        # 兜底: 任何新增 Tool 都先走这里 ——
        # 这种情况下 narrative 极易触发 fallback,但任务不会因为 builder 缺失而崩。
        return GenericToolContextBuilder(tool_name)
    return builder


__all__ = [
    "TOOL_NARRATIVE_CONTEXT_BUILDERS",
    "get_tool_context_builder",
    "GenericToolContextBuilder",
    "RequirementParserContextBuilder",
]
