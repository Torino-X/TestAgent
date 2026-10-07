"""Build user-facing PublicExecutionUpdate instances from real tool results.

Design rules followed:

1. Deterministic — pure functions over ``data`` dict, no LLM call.
2. Honest — counts come from real fields (length / .get).
3. Sanitized — no raw paths, URLs, tokens, internal storage paths.
4. Boundary-safe — every operation wrapped so a malformed ``data``
   dict never raises; the orchestrator must not be affected.

The orchestrator imports :func:`build_for_tool_result` and
:func:`build_for_retry` and attaches the returned dict to the
existing ``payload_json``.  No new SSE event is introduced.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .public_execution_update import PublicExecutionUpdate

_MAX_HEADLINE = 32
_MAX_SUMMARY = 200
_MAX_IMPACT = 120
_MAX_NEXT_ACTION = 80
_MAX_DETAIL_ITEM = 80
_MAX_DETAILS = 6
_SENSITIVE_MARKERS = (
    "/workspace/",
    "/tmp/",
    "C:\\",
    "D:\\",
    "/Users/",
    "/root/",
    "/var/",
    "secret",
    "token=",
    "api_key=",
    "http://",
    "https://",
)


def _truncate(text: str, limit: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def _safe_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _flatten_dict(value: Any, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        items: list[str] = []
        for key, inner in value.items():
            label = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
            items.extend(_flatten_dict(inner, label))
        return items
    if isinstance(value, list):
        items = []
        for index, inner in enumerate(value[:5]):
            items.extend(_flatten_dict(inner, f"{prefix}[{index}]"))
        return items
    return [f"{prefix}={value}"] if prefix else []


def _sanitize_text(text: str) -> str:
    cleaned = text
    close_brace = "]"
    for marker in _SENSITIVE_MARKERS:
        needle = marker + r"[^\s,;)" + close_brace + r"]*"
        if marker in cleaned:
            cleaned = re.sub(needle, "[已脱敏]", cleaned)
    return cleaned


def _sanitize_details(details: list[str]) -> tuple[str, ...]:
    safe: list[str] = []
    for item in details[:_MAX_DETAILS]:
        text = _sanitize_text(str(item))
        safe.append(_truncate(text, _MAX_DETAIL_ITEM))
    return tuple(safe)


def _build(
    kind: str,
    level: str,
    headline: str,
    summary: str,
    impact: str,
    next_action: str,
    tool_name: str,
    success: bool,
    details: list[str] | None = None,
    dedupe_suffix: str | None = None,
) -> PublicExecutionUpdate:
    base_key = f"{tool_name}:{kind}"
    dedupe = f"{base_key}:{dedupe_suffix}" if dedupe_suffix else base_key
    return PublicExecutionUpdate(
        version=1,
        kind=kind,
        level=level,
        headline=_truncate(headline, _MAX_HEADLINE),
        summary=_truncate(summary, _MAX_SUMMARY),
        impact=_truncate(impact, _MAX_IMPACT),
        next_action=_truncate(next_action, _MAX_NEXT_ACTION),
        details=_sanitize_details(details or []),
        source="template",
        dedupe_key=dedupe,
    )


# ── Per-tool success formatters ──────────────────────────────────────────────


def _req_parser_success(data: dict[str, Any]) -> PublicExecutionUpdate:
    structure = data.get("document_structure") or []
    tables = data.get("table_summaries") or []
    images = _safe_count(data.get("image_texts"))
    text_chars = len(data.get("text_content") or "")
    document_title = str(data.get("document_title") or "")
    details: list[str] = []
    if document_title:
        details.append(f"文档标题：{_truncate(document_title, 40)}")
    details.append(f"识别章节 {len(structure)} 个，关键表格 {len(tables)} 个")
    if images:
        details.append(f"含 {images} 张图片的关键文本")
    if text_chars:
        details.append(f"原文约 {text_chars // 100}×100 字")
    return _build(
        kind="tool_result",
        level="success",
        headline="需求文档解析完成",
        summary="已完成需求文档解析，并识别主要业务结构和表单/图片内容。",
        impact="解析结果将用于确定测试范围以及测试方案章节结构。",
        next_action="接下来将解析测试方案模板。",
        tool_name="RequirementParserTool",
        success=True,
        details=details,
        dedupe_suffix="success",
    )


def _tpl_parser_success(data: dict[str, Any]) -> PublicExecutionUpdate:
    sections = data.get("sections") or []
    table_count = sum(_safe_count(s.get("table_schemas")) for s in sections if isinstance(s, dict))
    ai = sum(_safe_count(s.get("ai_fields")) for s in sections if isinstance(s, dict))
    keep = sum(_safe_count(s.get("keep_sections")) for s in sections if isinstance(s, dict))
    template_name = str(data.get("template_name") or "")
    details: list[str] = []
    if template_name:
        details.append(f"模板：{_truncate(template_name, 40)}")
    details.append(f"识别模板章节 {len(sections)} 个，内嵌表格 {table_count} 个")
    if ai or keep:
        details.append(f"AI 生成候选 {ai} 项，保留原文候选 {keep} 项")
    return _build(
        kind="tool_result",
        level="success",
        headline="测试方案模板解析完成",
        summary="已完成模板解析，识别章节结构与已固化表格。",
        impact="模板结构将作为后续章节策略与生成边界。",
        next_action="接下来将匹配知识库与生成章节处理建议。",
        tool_name="TemplateParserTool",
        success=True,
        details=details,
        dedupe_suffix="success",
    )


def _kb_search_success(data: dict[str, Any]) -> PublicExecutionUpdate:
    # BUG FIX 2026-08-18 (方案 2):降级成功(未配置/session 缺失/上游失败)
    # → level="warning",让前端把这条渲染成黄色"降级完成"而非绿色"成功",
    # 也**不会**被 timecheck 当作 tool_failed。
    # 注意:degraded payload 也带 skip_reason 字段(_build_degraded_payload),
    # 因此 degraded 分支必须放在 skip_reason 分支之前。
    if data.get("degraded"):
        error_code = str(data.get("error_code") or "degraded")
        error_message = str(data.get("error_message") or "知识库检索降级完成。")
        return _build(
            kind="tool_result",
            level="warning",
            headline="知识库检索降级完成",
            summary="知识库不可用，测试方案将基于本地上下文生成，不影响主流程。",
            impact="不引用历史项目与标准条款，仅基于本地上下文生成。",
            next_action="接下来继续生成测试方案。",
            tool_name="KnowledgeSearchTool",
            success=True,
            details=[
                f"原因：{error_code} · {_truncate(error_message, 80)}",
            ],
            dedupe_suffix="degraded",
        )
    if data.get("skip_reason"):
        return _build(
            kind="tool_result",
            level="info",
            headline="知识库检索已跳过",
            summary="本次任务未启用知识库检索，将直接生成测试方案。",
            impact="不引用历史项目与标准条款，仅基于本地上下文生成。",
            next_action="接下来继续生成测试方案。",
            tool_name="KnowledgeSearchTool",
            success=True,
            details=[f"原因：{_truncate(str(data.get('skip_reason')), 60)}"],
            dedupe_suffix="disabled",
        )
    hit = _safe_count(data.get("similar_projects")) + _safe_count(data.get("standards")) + _safe_count(data.get("terms"))
    confidence = str(data.get("confidence") or "")
    details = [
        f"命中参考 {hit} 条",
    ]
    if confidence:
        details.append(f"综合置信度：{confidence}")
    elapsed_ms = data.get("elapsed_ms")
    if isinstance(elapsed_ms, (int, float)) and elapsed_ms > 0:
        details.append(f"耗时约 {int(elapsed_ms)} ms")
    return _build(
        kind="tool_result",
        level="success",
        headline="知识库检索完成",
        summary="已从历史项目与标准条款中检索相似参考。",
        impact="参考材料将用于丰富测试方案中的术语与样例。",
        next_action="接下来将形成章节处理建议。",
        tool_name="KnowledgeSearchTool",
        success=True,
        details=details,
        dedupe_suffix="success",
    )


def _section_success(data: dict[str, Any]) -> PublicExecutionUpdate:
    sections = data.get("sections") or []
    if not isinstance(sections, list):
        sections = []
    ai = sum(1 for s in sections if isinstance(s, dict) and s.get("suggested_action") == "ai_generate")
    keep = sum(1 for s in sections if isinstance(s, dict) and s.get("suggested_action") == "keep_template")
    manual = sum(1 for s in sections if isinstance(s, dict) and s.get("suggested_action") == "manual_fill")
    skipped = sum(1 for s in sections if isinstance(s, dict) and s.get("suggested_action") == "skip")
    user_constrained = sum(1 for s in sections if isinstance(s, dict) and s.get("constraint_source") == "user_prompt")
    details = [
        f"建议 AI 生成 {ai} 章、保留模板 {keep} 章、手动补充 {manual} 章、不生成 {skipped} 章",
    ]
    if user_constrained:
        details.append(f"其中 {user_constrained} 个章节按你的提示词指定")
    return _build(
        kind="tool_result",
        level="success",
        headline="章节处理建议已生成",
        summary="已为每个章节推荐处理方式，等待你确认。",
        impact="确认后的策略将驱动后续章节生成和产物导出。",
        next_action="请确认章节处理策略。",
        tool_name="SectionSuggestionTool",
        success=True,
        details=details,
        dedupe_suffix="success",
    )


def _gen_success(data: dict[str, Any]) -> PublicExecutionUpdate:
    generated = _safe_count(data.get("generated_sections"))
    kept = _safe_count(data.get("kept_sections"))
    manual = _safe_count(data.get("manual_sections"))
    tables = _safe_count(data.get("tables_generated"))
    word_count = data.get("total_word_count")
    details = [f"新增 {generated} 章、保留 {kept} 章、手动补充 {manual} 章"]
    if tables:
        details.append(f"新增表格 {tables} 个")
    if isinstance(word_count, (int, float)) and word_count > 0:
        details.append(f"总字数约 {int(word_count // 100) * 100}")
    return _build(
        kind="tool_result",
        level="success",
        headline="测试方案生成完成",
        summary="已按章节策略生成测试方案正文与表格。",
        impact="生成结果将进入审查环节。",
        next_action="接下来将进入结果审查。",
        tool_name="TestPlanGeneratorTool",
        success=True,
        details=details,
        dedupe_suffix="success",
    )


def _review_success(data: dict[str, Any]) -> PublicExecutionUpdate:
    level = str(data.get("level") or "")
    passed = bool(data.get("passed"))
    issues = _safe_count(data.get("issues"))
    suggestions = _safe_count(data.get("suggestions"))
    missing = _safe_count(data.get("missing_sections"))
    empty = _safe_count(data.get("empty_sections"))
    placeholder = _safe_count(data.get("placeholder_sections"))
    details = [
        f"覆盖 {_safe_count(data.get('covered_module_count'))}/{_safe_count(data.get('total_module_count'))} 个业务模块",
    ]
    if missing or empty or placeholder:
        details.append(f"待补 {missing} 章、空段 {empty} 章、占位 {placeholder} 章")
    if issues:
        details.append(f"规则问题 {issues} 条，建议 {suggestions} 条")
    if level == "warning":
        summary = "审查通过，但存在可改进项。"
        head = "审查通过（含建议）"
        impact = "建议将进入下一轮优化（若已开启自动重试则自动重试一次）。"
        next_action = "如需继续，请直接等待自动进入下一步。"
        final_level = "warning"
    elif level == "failed" or not passed:
        summary = "审查未通过，已记录主要问题。"
        head = "审查未通过"
        impact = "将依据规则尝试一次自动重写。"
        next_action = "请关注下一步是否提示重新生成。"
        final_level = "warning"
    else:
        summary = "审查通过，未发现明显问题。"
        head = "审查通过"
        impact = "方案可进入产物导出阶段。"
        next_action = "接下来导出 Word 文档。"
        final_level = "success"
    return _build(
        kind="tool_result",
        level=final_level,
        headline=head,
        summary=summary,
        impact=impact,
        next_action=next_action,
        tool_name="ResultReviewTool",
        success=True,
        details=details,
        dedupe_suffix=f"success:{level or 'passed'}",
    )


def _regen_success(data: dict[str, Any]) -> PublicExecutionUpdate:
    fixed = _safe_count(data.get("fixed_count"))
    issue = _safe_count(data.get("issue_count"))
    details = [f"针对 {issue} 条问题，已修复 {fixed} 条"]
    return _build(
        kind="tool_result",
        level="success",
        headline="方案重写完成",
        summary="针对审查点已对受影响章节进行重写。",
        impact="重写章节将重新进入审查。",
        next_action="接下来重新进入审查。",
        tool_name="TestPlanRegenTool",
        success=True,
        details=details,
        dedupe_suffix="success",
    )


def _integrity_detail(value: Any) -> str:
    """Convert the exporter integrity result into stable user-facing text."""
    if isinstance(value, bool):
        return "完整性自检：通过" if value else "完整性自检：未通过"
    if isinstance(value, dict):
        passed = value.get("passed")
        if isinstance(passed, bool):
            return "完整性自检：通过" if passed else "完整性自检：未通过"
    return ""


def _export_success(data: dict[str, Any]) -> PublicExecutionUpdate:
    file_name = str(data.get("file_name") or "")
    integrity = _integrity_detail(data.get("integrity"))
    file_ext = str(data.get("file_ext") or "")
    warnings = data.get("warnings")
    details: list[str] = []
    if file_name:
        details.append(f"产物文件名：{_truncate(file_name, 40)}")
    if file_ext:
        details.append(f"文件类型：{file_ext}")
    if integrity:
        details.append(integrity)
    warning_count = _safe_count(warnings)
    if warning_count:
        details.append(f"格式自检附 {warning_count} 项提示（非阻塞）")
    # WordExport success ≠ task success — next action says "继续等结果审查"
    return _build(
        kind="tool_result",
        level="success",
        headline="Word 文档导出完成",
        summary="已把测试方案渲染为 Word 文件，等待用户下载。",
        impact="导出的产物将出现在产物区，可直接下载。",
        next_action="接下来进行格式自检（如有丢失再提示确认）。",
        tool_name="WordExportTool",
        success=True,
        details=details,
        dedupe_suffix="success",
    )


def _format_check_success(data: dict[str, Any]) -> PublicExecutionUpdate:
    level = str(data.get("level") or "")
    drift = data.get("drift")
    losses = data.get("losses") or []
    details: list[str] = []
    if isinstance(drift, (int, float)):
        details.append(f"整体偏差 {drift:.2%}" if drift < 1 else f"整体偏差 {drift:.2f}")
    if losses:
        details.append(f"格式警告 {len(losses)} 项")
    if level == "passed":
        head = "格式自检通过"
        summary = "文档格式与模板保持一致，未发现用户可见丢失。"
        impact = "可以直接交付产物。"
        action = "可以查看产物并下载。"
        lvl = "success"
    elif level == "warning":
        head = "格式自检通过（含建议）"
        summary = "文档格式整体一致，但存在可改进项。"
        impact = "建议可在下一轮产物中迭代。"
        action = "继续等待后续步骤完成。"
        lvl = "success"
    else:
        head = "格式自检通过"
        summary = "格式自检已完成。"
        impact = "如有关键丢失会另行提示。"
        action = "继续等待后续步骤完成。"
        lvl = "success"
    return _build(
        kind="tool_result",
        level=lvl,
        headline=head,
        summary=summary,
        impact=impact,
        next_action=action,
        tool_name="DocxFormatCheckTool",
        success=True,
        details=details,
        dedupe_suffix=f"success:{level or 'unknown'}",
    )


# ── Per-tool failure formatters ──────────────────────────────────────────────


def _generic_failure(
    tool_name: str,
    headline: str,
    impact: str,
    data: dict[str, Any],
    error: Any,
) -> PublicExecutionUpdate:
    err_text = ""
    if isinstance(error, dict):
        err_text = str(error.get("message") or error.get("code") or "")
    elif error is not None:
        err_text = str(error)
    details: list[str] = []
    if isinstance(error, dict):
        code = error.get("code")
        if code:
            details.append(f"错误码：{code}")
        recoverable = error.get("recoverable")
        if recoverable is not None:
            details.append(f"可重试：{'是' if recoverable else '否'}")
    elif error is not None:
        details.append(f"原因：{_truncate(err_text, 60)}")
    return _build(
        kind="tool_result",
        level="warning",
        headline=headline,
        summary="本工具本次未完成。",
        impact=impact,
        next_action="系统将依据规则自动决定是否重试或降级。",
        tool_name=tool_name,
        success=False,
        details=details,
        dedupe_suffix="failed",
    )


def _req_failure(data: dict[str, Any], error: Any) -> PublicExecutionUpdate:
    return _generic_failure(
        "RequirementParserTool",
        "需求文档解析未完成",
        "将依据错误类型决定是重试还是后续降级提示。",
        data,
        error,
    )


def _tpl_failure(data: dict[str, Any], error: Any) -> PublicExecutionUpdate:
    return _generic_failure(
        "TemplateParserTool",
        "测试方案模板解析未完成",
        "无法解析模板时将影响章节结构识别。",
        data,
        error,
    )


def _kb_failure(data: dict[str, Any], error: Any) -> PublicExecutionUpdate:
    return _generic_failure(
        "KnowledgeSearchTool",
        "知识库检索未完成",
        "知识库失败时，方案将仅基于本地上下文生成，不影响主流程。",
        data,
        error,
    )


def _section_failure(data: dict[str, Any], error: Any) -> PublicExecutionUpdate:
    return _generic_failure(
        "SectionSuggestionTool",
        "章节处理建议未生成",
        "未生成建议前不会进入方案正文生成。",
        data,
        error,
    )


def _gen_failure(data: dict[str, Any], error: Any) -> PublicExecutionUpdate:
    return _generic_failure(
        "TestPlanGeneratorTool",
        "测试方案生成未完成",
        "将依据规则进入自动重试或自动降级。",
        data,
        error,
    )


def _review_failure(data: dict[str, Any], error: Any) -> PublicExecutionUpdate:
    return _generic_failure(
        "ResultReviewTool",
        "结果审查未完成",
        "审查失败时可能跳过自动重写步骤。",
        data,
        error,
    )


def _regen_failure(data: dict[str, Any], error: Any) -> PublicExecutionUpdate:
    return _generic_failure(
        "TestPlanRegenTool",
        "方案重写未完成",
        "重写失败时会保留原始章节继续导出。",
        data,
        error,
    )


def _export_failure(data: dict[str, Any], error: Any) -> PublicExecutionUpdate:
    return _generic_failure(
        "WordExportTool",
        "文档导出未完成",
        "导出失败时产物区不会出现新文件，请等待重试或重试任务。",
        data,
        error,
    )


def _format_check_failure(data: dict[str, Any], error: Any) -> PublicExecutionUpdate:
    return _generic_failure(
        "DocxFormatCheckTool",
        "格式自检未完成",
        "格式自检失败时不会拦截产物交付，仅在丢失显著时另行提示。",
        data,
        error,
    )


# ── Retry formatters ────────────────────────────────────────────────────────


_RETRY_VERB = {
    "schema_feedback": "提示词规范反馈",
    "reflect_feedback": "让模型自检后再生成",
    "chunked_split": "分块重试",
    "degrade": "降级输入重试",
    "degrade_to_template": "降级到模板原文",
    "same_inputs": "原参数重试",
    "backoff": "退避后重试",
    "hard_stop": "已硬停",
}


def _retry_verb(strategy: str) -> str:
    return _RETRY_VERB.get(strategy, f"应用{strategy}策略")


def _build_retry(
    tool_name: str,
    strategy: str,
    attempt: int,
    last_error: str,
    next_attempt_in_seconds: float | None = None,
) -> PublicExecutionUpdate:
    verb = _retry_verb(strategy)
    details = []
    if last_error:
        details.append(f"上次原因：{_truncate(last_error, 60)}")
    if isinstance(next_attempt_in_seconds, (int, float)) and next_attempt_in_seconds > 0:
        details.append(f"距下一次约 {int(next_attempt_in_seconds)}s")
    return _build(
        kind="tool_retry",
        level="retrying",
        headline=f"正在第 {attempt} 次重试",
        summary=f"系统正在用「{_truncate(verb, 24)}」策略重新尝试。",
        impact="不会修改已经得到的结果。",
        next_action="请稍候，结果出来后会再次刷新状态。",
        tool_name=tool_name,
        success=False,
        details=details,
        dedupe_suffix=f"{strategy}:{attempt}",
    )


# ── Unknown-tool fallback ───────────────────────────────────────────────────


def _unknown_success(tool_name: str, data: dict[str, Any]) -> PublicExecutionUpdate:
    return _build(
        kind="tool_result",
        level="success",
        headline=f"{_truncate(tool_name, 18)}执行完成",
        summary="工具已返回结果。",
        impact="结果将进入下一步处理。",
        next_action="继续等待后续步骤。",
        tool_name=tool_name,
        success=True,
        details=[],
        dedupe_suffix="success",
    )


def _unknown_failure(tool_name: str, data: dict[str, Any], error: Any) -> PublicExecutionUpdate:
    return _generic_failure(
        tool_name,
        f"{_truncate(tool_name, 18)}未完成",
        "系统将依据规则自动决定是否重试。",
        data,
        error,
    )


# ── Dispatch tables ─────────────────────────────────────────────────────────

_SUCCESS_FORMATTERS: dict[str, Callable[[dict[str, Any]], PublicExecutionUpdate]] = {
    "RequirementParserTool": _req_parser_success,
    "TemplateParserTool": _tpl_parser_success,
    "KnowledgeSearchTool": _kb_search_success,
    "SectionSuggestionTool": _section_success,
    "TestPlanGeneratorTool": _gen_success,
    "ResultReviewTool": _review_success,
    "TestPlanRegenTool": _regen_success,
    "WordExportTool": _export_success,
    "DocxFormatCheckTool": _format_check_success,
}

_FAILURE_FORMATTERS: dict[str, Callable[[dict[str, Any], Any], PublicExecutionUpdate]] = {
    "RequirementParserTool": _req_failure,
    "TemplateParserTool": _tpl_failure,
    "KnowledgeSearchTool": _kb_failure,
    "SectionSuggestionTool": _section_failure,
    "TestPlanGeneratorTool": _gen_failure,
    "ResultReviewTool": _review_failure,
    "TestPlanRegenTool": _regen_failure,
    "WordExportTool": _export_failure,
    "DocxFormatCheckTool": _format_check_failure,
}


def build_for_tool_result(
    tool_name: str,
    success: bool,
    data: dict[str, Any] | None,
    error: Any = None,
) -> PublicExecutionUpdate:
    """Build a public update for ``TOOL_FINISHED`` / ``TOOL_FAILED``.

    ``data`` is the ``result.data`` dict (already deserialized by the
    tool itself); ``error`` is the ``result.error`` dict on failure or
    ``None``.  Unknown tools fall back to the generic formatter so the
    orchestrator always gets a non-None result.
    """
    payload = data if isinstance(data, dict) else {}
    try:
        if success:
            formatter = _SUCCESS_FORMATTERS.get(tool_name)
            if formatter is not None:
                return formatter(payload)
            return _unknown_success(tool_name or "UnknownTool", payload)
        formatter = _FAILURE_FORMATTERS.get(tool_name)
        if formatter is not None:
            return formatter(payload, error)
        return _unknown_failure(tool_name or "UnknownTool", payload, error)
    except Exception:
        # Boundary: never let a builder crash the orchestrator.
        return _build(
            kind="tool_result",
            level="success" if success else "warning",
            headline="处理完成",
            summary="已收到该步骤的反馈。",
            impact="继续等待后续步骤。",
            next_action="继续等待后续步骤。",
            tool_name=tool_name or "UnknownTool",
            success=success,
            details=[],
            dedupe_suffix="fallback" if success else "failed-fallback",
        )


_TOOL_DISPLAY_INPUTS: dict[str, str] = {
    "RequirementParserTool": "读取需求文档并提取结构",
    "TemplateParserTool": "读取测试方案模板并提取章节结构",
    "KnowledgeSearchTool": "基于当前任务上下文检索知识库",
    "SectionSuggestionTool": "根据已解析内容生成章节处理建议",
    "TestPlanGeneratorTool": "根据确认的章节策略生成测试方案",
    "TestPlanRegenTool": "根据审查意见重新生成指定章节",
    "ResultReviewTool": "审查已生成测试方案的完整性和一致性",
    "WordExportTool": "导出测试方案 Word 文档",
    "DocxFormatCheckTool": "校验导出文档的结构和格式",
}


def build_tool_display_input(tool_name: str, _inputs: dict[str, Any] | None = None) -> str:
    """Return a safe, user-facing input summary for a tool event."""
    return _TOOL_DISPLAY_INPUTS.get(tool_name, "使用当前任务上下文")


def build_tool_display_output(
    update: PublicExecutionUpdate,
    *,
    success: bool,
) -> str:
    """Return the concise, already-sanitised output line for a tool log."""
    text = str(getattr(update, "summary", "") or getattr(update, "headline", "")).strip()
    if text:
        return text
    return "工具已完成，详细结果见执行说明" if success else "工具执行失败，请查看错误说明"


def split_into_chunks(
    full_update: PublicExecutionUpdate,
) -> list[PublicExecutionUpdate]:
    """Split a fully built ``PublicExecutionUpdate`` into progressive frames.

    Streaming layout (Section 24-ext):

    - ``tool_result`` success / info: 5 frames — headline → +summary →
      +impact → +next_action → +details[].
    - ``tool_result`` warning: 2 frames — headline → +summary+details.
    - ``tool_failed``: 2 frames — headline → +summary.
    - ``tool_retry``: 2 frames — headline → +summary.

    Every frame except the last carries ``chunk_final=False``; only the
    last frame in the returned list sets ``chunk_final=True`` so legacy
    consumers that ignore the new fields still see the canonical card
    on the final publish.

    The original ``full_update`` is returned inside the last frame
    untouched, so callers can keep a reference to the canonical event.
    """
    try:
        if full_update is None:
            return []

        frames_total = _chunk_frame_count(full_update)
        details = full_update.details
        summary = full_update.summary
        impact = full_update.impact
        next_action = full_update.next_action
        headline = full_update.headline

        if full_update.kind == "tool_retry":
            return [
                _make_frame(
                    full_update,
                    chunk_index=0,
                    chunk_total=frames_total,
                    chunk_final=False,
                    headline=headline,
                    summary="",
                    impact="",
                    next_action="",
                    details=(),
                ),
                _make_frame(
                    full_update,
                    chunk_index=1,
                    chunk_total=frames_total,
                    chunk_final=True,
                    headline=headline,
                    summary=summary,
                    impact=impact,
                    next_action=next_action,
                    details=details,
                ),
            ]

        if full_update.kind == "tool_failed" or full_update.level == "warning":
            return [
                _make_frame(
                    full_update,
                    chunk_index=0,
                    chunk_total=frames_total,
                    chunk_final=False,
                    headline=headline,
                    summary="",
                    impact="",
                    next_action="",
                    details=(),
                ),
                _make_frame(
                    full_update,
                    chunk_index=1,
                    chunk_total=frames_total,
                    chunk_final=True,
                    headline=headline,
                    summary=summary,
                    impact=impact,
                    next_action=next_action,
                    details=details,
                ),
            ]

        return [
            _make_frame(
                full_update,
                chunk_index=0,
                chunk_total=frames_total,
                chunk_final=False,
                headline=headline,
                summary="",
                impact="",
                next_action="",
                details=(),
            ),
            _make_frame(
                full_update,
                chunk_index=1,
                chunk_total=frames_total,
                chunk_final=False,
                headline=headline,
                summary=summary,
                impact="",
                next_action="",
                details=(),
            ),
            _make_frame(
                full_update,
                chunk_index=2,
                chunk_total=frames_total,
                chunk_final=False,
                headline=headline,
                summary=summary,
                impact=impact,
                next_action="",
                details=(),
            ),
            _make_frame(
                full_update,
                chunk_index=3,
                chunk_total=frames_total,
                chunk_final=False,
                headline=headline,
                summary=summary,
                impact=impact,
                next_action=next_action,
                details=(),
            ),
            _make_frame(
                full_update,
                chunk_index=4,
                chunk_total=frames_total,
                chunk_final=True,
                headline=headline,
                summary=summary,
                impact=impact,
                next_action=next_action,
                details=details,
            ),
        ]
    except Exception:
        return [full_update]


def _chunk_frame_count(full_update: PublicExecutionUpdate) -> int:
    if full_update.kind == "tool_retry":
        return 2
    if full_update.kind == "tool_failed" or full_update.level == "warning":
        return 2
    return 5


def _make_frame(
    full_update: PublicExecutionUpdate,
    *,
    chunk_index: int,
    chunk_total: int,
    chunk_final: bool,
    headline: str,
    summary: str,
    impact: str,
    next_action: str,
    details: tuple[str, ...],
) -> PublicExecutionUpdate:
    return PublicExecutionUpdate(
        version=full_update.version,
        kind=full_update.kind,
        level=full_update.level,
        headline=headline,
        summary=summary,
        impact=impact,
        next_action=next_action,
        details=details,
        source=full_update.source,
        dedupe_key=full_update.dedupe_key,
        chunk_index=chunk_index,
        chunk_total=chunk_total,
        chunk_final=chunk_final,
    )


def build_for_retry(
    tool_name: str,
    strategy: str,
    attempt: int,
    last_error: str = "",
    next_attempt_in_seconds: float | None = None,
) -> PublicExecutionUpdate:
    """Build a public update for ``RETRYING`` events."""
    try:
        return _build_retry(
            tool_name=tool_name or "UnknownTool",
            strategy=strategy or "backoff",
            attempt=max(1, int(attempt)),
            last_error=last_error or "",
            next_attempt_in_seconds=next_attempt_in_seconds,
        )
    except Exception:
        return _build(
            kind="tool_retry",
            level="retrying",
            headline=f"正在第 {attempt} 次重试",
            summary="系统在尝试重试。",
            impact="不会修改已经得到的结果。",
            next_action="请稍候。",
            tool_name=tool_name or "UnknownTool",
            success=False,
            details=[],
            dedupe_suffix="fallback",
        )


# 模块定位:Public Execution Update Builder — 构造 envelope 的统一助手
#
# 把工具生命周期事件 (started / progress / finished / failed / retry) 转成
# 公开的 envelope(Pydantic model),供 SSE 推送 + DB 持久化。
#
# 链路:
#   graph node 调 build_for_tool_result(...) / build_for_retry(...)
#     → 返回 envelope {type: TOOL_FINISHED, data: PublicExecutionUpdate(...)}
#     → LiveEventBus → SSE 推前端
#
# 关键约束:
#   - 字段白名单(no stack_trace / no internal_id / no api_key);
#   - split_into_chunks 长 narrative_text 切成多帧,避免单 SSE 帧过大;
#   - 任何 field 超过限制 → truncate + 加 ...;
#   - 失败兜底返回最小 envelope(fallback_to_default=True);
#   - 升级到 Phase 2.9B:narrative 字段由 NarrativeComposer LLM 生成,
#     这层 builder 仅负责 envelope wire format。
