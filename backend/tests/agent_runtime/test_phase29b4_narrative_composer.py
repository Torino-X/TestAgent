"""Phase 2.9B.4 — Narrative Composer 组件测试。

覆盖:
  - NarrativeStreamDecoder(正常流 / 单字符 chunk / 标签拆分 / 中文 / 多 DETAIL /
    非法标签 / 缺闭合 / 缺字段 / 标签外文本 / 空字段);
  - NarrativeValidator(headline/summary/impact/next_action 非空、details 2~5、
    数字白名单、文件名白名单、状态语义、敏感过滤);
  - Context Builder(白名单事实、无敏感字段、数字/文件名约束);
  - NarrativeComposer(成功 / fallback / 事件序列);
  - Barrier 同步顺序(无 create_task;tool_finished < narrative < next tool);
  - 幂等逻辑键。
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from app.agent_runtime._shared.public_narrative import AgentPublicUpdateDraft
from app.agent_runtime.narrative_composer.composer import NarrativeComposer
from app.agent_runtime.narrative_composer.context_builders import (
    RequirementParserContextBuilder,
    get_tool_context_builder,
)
from app.agent_runtime.narrative_composer.prompts import (
    build_task_summary_prompt,
    build_tool_narrative_prompt,
)
from app.agent_runtime.narrative_composer.schemas import (
    NarrativeFactConstraints,
    TaskSummaryNarrativeContext,
    ToolNarrativeContext,
)
from app.agent_runtime.narrative_composer.stream_decoder import (
    NarrativeStreamDecoder,
    StreamDecodeError,
)
from app.agent_runtime.narrative_composer.validator import NarrativeValidator

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

_FULL = (
    "<HEADLINE>需求文档解析已完成</HEADLINE>"
    "<SUMMARY>已识别主要业务结构。</SUMMARY>"
    "<IMPACT>用于确定测试范围。</IMPACT>"
    "<NEXT_ACTION>接下来解析模板。</NEXT_ACTION>"
    "<DETAIL>识别章节 43 个</DETAIL>"
    "<DETAIL>识别表格 7 个</DETAIL>"
)


# ── 1. NarrativeStreamDecoder ───────────────────────────────────────────


class TestStreamDecoder:
    def test_natural_narrative_stream(self):
        d = NarrativeStreamDecoder()
        text = (
            "<NARRATIVE>"
            "我已经把需求文档拆成可用的测试范围了：这次识别到 43 个章节和 7 张表格，"
            "后面生成测试方案时可以直接沿着这些业务结构往下走。"
            "</NARRATIVE>"
        )

        deltas = d.feed(text)
        d.finish()
        pub = d.public_update_dict()

        assert [x.field for x in deltas] == ["narrative_text"]
        assert pub["narrative_text"].startswith("我已经把需求文档拆成可用的测试范围了")
        assert pub["headline"]
        assert pub["summary"] == pub["narrative_text"]

    def test_normal_stream_one_shot(self):
        d = NarrativeStreamDecoder()
        deltas = d.feed(_FULL)
        d.finish()
        assert [x.field for x in deltas] == [
            "headline", "summary", "impact", "next_action", "details", "details",
        ]
        pub = d.public_update_dict()
        assert pub["headline"] == "需求文档解析已完成"
        assert pub["summary"] == "已识别主要业务结构。"
        assert pub["impact"] == "用于确定测试范围。"
        assert pub["next_action"] == "接下来解析模板。"
        assert pub["details"] == ["识别章节 43 个", "识别表格 7 个"]

    def test_single_char_chunks(self):
        d = NarrativeStreamDecoder()
        all_deltas = []
        for ch in _FULL:
            all_deltas += d.feed(ch)
        d.finish()
        assert d.public_update_dict()["headline"] == "需求文档解析已完成"
        assert len(all_deltas) >= 5

    def test_tag_split_across_chunks(self):
        d = NarrativeStreamDecoder()
        chunks = [
            "<HEAD", "LINE>需求文档", "解析已完成", "</HEADLINE>",
            "<SUMMARY>识别结构。</SUMMARY>",
            "<IMPACT>影响范围。</IMPACT>",
            "<NEXT_ACTION>解析模板。</NEXT_ACTION>",
            "<DETAIL>章节 43 个</DETAIL><DETAIL>表格 7 个</DETAIL>",
        ]
        for c in chunks:
            d.feed(c)
        d.finish()
        assert d.public_update_dict()["summary"] == "识别结构。"

    def test_missing_closing_tag_raises(self):
        d = NarrativeStreamDecoder()
        d.feed("<HEADLINE>未闭合")
        with pytest.raises(StreamDecodeError):
            d.finish()

    def test_missing_required_field_raises(self):
        d = NarrativeStreamDecoder()
        d.feed("<HEADLINE>只有标题</HEADLINE><SUMMARY>s</SUMMARY>")
        with pytest.raises(StreamDecodeError):
            d.finish()

    def test_outside_text_rejected(self):
        d = NarrativeStreamDecoder()
        d.feed("非法正文<HEADLINE>h</HEADLINE><SUMMARY>s</SUMMARY><IMPACT>i</IMPACT><NEXT_ACTION>n</NEXT_ACTION>")
        d.finish()
        assert d.outside_text_seen is True

    def test_provider_returns_all_at_once(self):
        d = NarrativeStreamDecoder()
        d.feed(_FULL)
        d.finish()
        assert d.public_update_dict()["details"] == ["识别章节 43 个", "识别表格 7 个"]


# ── 2. NarrativeValidator ───────────────────────────────────────────────


def _ctx(status="success", allowed_nums=None, allowed_files=None):
    return ToolNarrativeContext(
        tool_name="RequirementParserTool",
        tool_call_id="tc",
        terminal_status=status,
        fact_constraints=NarrativeFactConstraints(
            allowed_numeric_facts=allowed_nums or [],
            allowed_file_names=allowed_files or [],
        ),
    )


class TestValidator:
    def test_natural_narrative_does_not_require_template_fields(self):
        v = NarrativeValidator()
        res = v.validate(
            public_update={
                "headline": "需求解析完成",
                "summary": "",
                "impact": "",
                "next_action": "",
                "details": [],
                "narrative_text": "我已经读完需求文档了，这次提取到 43 个章节和 7 张表格，接下来可以按这些结构生成测试方案。",
            },
            tool_context=_ctx(allowed_nums=[43, 7]),
        )

        assert res.valid is True, res.errors

    def test_valid_full_public_update(self):
        v = NarrativeValidator()
        res = v.validate(
            public_update={
                "headline": "H", "summary": "S", "impact": "I", "next_action": "N",
                "details": ["a", "b"],
            },
            tool_context=_ctx(allowed_nums=[43, 7]),
        )
        assert res.valid is True, res.errors

    def test_missing_body_field_invalid(self):
        v = NarrativeValidator()
        res = v.validate(
            public_update={"headline": "H", "summary": "S", "impact": "I", "next_action": ""},
        )
        assert res.valid is False
        assert any("next_action" in e for e in res.errors)

    def test_fabricated_number_rejected(self):
        v = NarrativeValidator()
        res = v.validate(
            public_update={
                "headline": "H", "summary": "S", "impact": "I", "next_action": "N",
                "details": ["识别章节 999 个"],
            },
            tool_context=_ctx(allowed_nums=[43, 7]),
        )
        assert res.valid is False

    def test_failed_tool_cannot_say_success(self):
        v = NarrativeValidator()
        res = v.validate(
            public_update={
                "headline": "解析成功", "summary": "成功完成", "impact": "I", "next_action": "N",
                "details": ["a", "b"],
            },
            tool_context=_ctx(status="failed"),
        )
        assert res.valid is False
        assert any("failed" in e for e in res.errors)

    def test_retry_planned_mentions_retry(self):
        v = NarrativeValidator()
        ctx = _ctx()
        ctx.execution_context = {"retry_planned": True, "waiting_for_user": False}
        res = v.validate(
            public_update={
                "headline": "H", "summary": "S", "impact": "I", "next_action": "继续",
                "details": ["a", "b"],
            },
            tool_context=ctx,
        )
        assert res.valid is False
        assert any("重试" in e for e in res.errors)

    def test_sensitive_field_rejected(self):
        v = NarrativeValidator()
        res = v.validate(
            public_update={
                "headline": "H", "summary": "S", "impact": "I", "next_action": "N",
                "details": ["Traceback (most recent call last)"],
            },
            tool_context=_ctx(),
        )
        assert res.valid is False

    def test_markdown_ordered_list_numbers_do_not_count_as_fabricated_facts(self):
        v = NarrativeValidator()
        ctx = TaskSummaryNarrativeContext(
            task_status="completed",
            generated_sections=16,
            review={"blocking_issues": 3, "warnings": 0, "suggestions": 1},
            artifact={"name": "plan.docx", "available": True},
            fact_constraints=NarrativeFactConstraints(
                allowed_numeric_facts=[16, 3, 1],
                allowed_file_names=["plan.docx"],
                allowed_literal_facts=["plan.docx"],
            ),
        )
        res = v.validate(
            public_update={
                "headline": "done",
                "summary": "",
                "impact": "",
                "next_action": "",
                "details": [],
                "narrative_text": (
                    "已生成 16 个章节，审查里有 3 个阻断问题和 1 条优化建议。\n\n"
                    "1. 阻断问题 A\n"
                    "2. 阻断问题 B\n"
                    "3. 阻断问题 C\n"
                    "4. 优化建议 D\n"
                ),
            },
            task_summary_context=ctx,
        )
        assert res.valid is True, res.errors


class TestNarrativePrompts:
    def test_tool_prompt_allows_markdown_and_avoids_fixed_templates(self):
        system_prompt, _ = build_tool_narrative_prompt(_ctx(allowed_nums=[3]))
        assert "Markdown" in system_prompt
        assert "不要输出 Markdown" not in system_prompt
        assert "影响 / 下一步 / 详情" not in system_prompt

    def test_task_summary_prompt_requires_issue_details_when_counts_exist(self):
        ctx = TaskSummaryNarrativeContext(
            task_status="completed",
            generated_sections=16,
            review={
                "blocking_issues": 3,
                "warnings": 0,
                "suggestions": 1,
                "blocking_issue_details": ["A", "B", "C"],
                "suggestion_details": ["D"],
            },
            artifact={"name": "plan.docx", "available": True},
        )
        system_prompt, _ = build_task_summary_prompt(ctx)
        assert "Markdown" in system_prompt
        assert "不要输出 Markdown" not in system_prompt
        assert "blocking_issue_details" in system_prompt
        assert "suggestion_details" in system_prompt


# ── 3. Context Builder ──────────────────────────────────────────────────


class TestContextBuilders:
    def test_requirement_parser_builder_whitelist(self):
        builder = get_tool_context_builder("RequirementParserTool")
        ctx = builder.build(
            graph_state={
                "requirement_analysis": {
                    "document_structure": [{"name": "s1"}, {"name": "s2"}],
                    "table_summaries": [{"title": "t1"}],
                    "image_texts": [{"text": "img1"}],
                    "document_title": "需求文档",
                    "recognized_image_count": 1,
                }
            },
            tool_call_id="RequirementParserTool-abc",
            source_event_id="evt-1",
            attempt=1,
            terminal_status="success",
            duration_ms=1000,
            continuation_route="parse_template",
        )
        assert ctx.output_facts["section_count"] == 2
        assert ctx.output_facts["table_count"] == 1
        assert 2 in ctx.fact_constraints.allowed_numeric_facts
        assert 1 in ctx.fact_constraints.allowed_numeric_facts
        assert ctx.tool_call_id == "RequirementParserTool-abc"
        assert ctx.execution_context["next_node"] == "parse_template"

    def test_unknown_tool_uses_generic(self):
        builder = get_tool_context_builder("MysteryTool")
        ctx = builder.build(
            graph_state={}, tool_call_id="t", source_event_id="e",
            attempt=1, terminal_status="success", duration_ms=1,
            continuation_route="next",
        )
        assert ctx.tool_name == "MysteryTool"
        assert not ctx.input_facts

    def test_builder_no_sensitive_fields(self):
        builder = get_tool_context_builder("RequirementParserTool")
        ctx = builder.build(
            graph_state={"requirement_analysis": {"api_key": "sk-secret"}},
            tool_call_id="t", source_event_id="e", attempt=1,
            terminal_status="success", duration_ms=1,
            continuation_route="next",
        )
        dumped = json.dumps(ctx.model_dump(), ensure_ascii=False)
        assert "sk-secret" not in dumped

    def test_requirement_parser_builder_whitelists_runtime_file_name_alias(self):
        builder = RequirementParserContextBuilder()
        runtime_name = "02_runtime_requirement_v3.docx"
        ctx = builder.build(
            graph_state={
                "requirement_file_id": "file_internal_123",
                "requirement_analysis": {
                    "file_name": runtime_name,
                    "document_structure": [{} for _ in range(43)],
                    "table_summaries": [{} for _ in range(7)],
                    "image_texts": [{} for _ in range(3)],
                },
            },
            tool_call_id="RequirementParserTool-tc",
            source_event_id="evt-final",
            attempt=1,
            terminal_status="success",
            duration_ms=100,
            continuation_route="parse_template",
        )

        assert ctx.input_facts["file_name"] == runtime_name
        assert runtime_name in ctx.fact_constraints.allowed_file_names
        assert runtime_name in ctx.fact_constraints.numeric_exempt_literals
        assert 43 in ctx.fact_constraints.allowed_numeric_facts
        assert 7 in ctx.fact_constraints.allowed_numeric_facts
        assert 3 in ctx.fact_constraints.allowed_numeric_facts

        validation = NarrativeValidator().validate(
            public_update={
                "headline": "Requirement parsed",
                "summary": f"Parsed {runtime_name} with 43 sections.",
                "impact": "The next template step can use the extracted scope.",
                "next_action": "Continue with the template parser.",
                "details": [
                    f"Source file is {runtime_name}.",
                    "Extracted 43 sections, 7 tables, and 3 image texts.",
                ],
            },
            tool_context=ctx,
        )
        assert validation.valid is True, validation.errors

    def test_requirement_parser_builder_whitelists_storage_prefixed_original_name(self):
        builder = RequirementParserContextBuilder()
        stored_name = "20260803_df6737bb_01_智慧校园预约与签到系统_需求说明书.docx"
        original_name = "01_智慧校园预约与签到系统_需求说明书.docx"
        ctx = builder.build(
            graph_state={
                "requirement_file_id": "file_64b77308",
                "requirement_analysis": {
                    "file_name": stored_name,
                    "document_structure": [{} for _ in range(43)],
                    "table_summaries": [{} for _ in range(7)],
                    "image_texts": [{} for _ in range(3)],
                },
            },
            tool_call_id="RequirementParserTool-tc",
            source_event_id="evt-final",
            attempt=1,
            terminal_status="success",
            duration_ms=71000,
            continuation_route="parse_template",
        )

        assert original_name in ctx.fact_constraints.allowed_file_names
        validation = NarrativeValidator().validate(
            public_update={
                "headline": "需求说明书解析完成",
                "summary": f"已成功解析{original_name}，提取出43个章节、7张表格及3张图片。",
                "impact": "需求文档结构化数据已就绪。",
                "next_action": "下一步将执行模板解析。",
                "details": [
                    f"解析对象为{original_name}",
                    "共识别出43个章节结构单元",
                    "文档中包含7张表格和3张图片资源",
                ],
            },
            tool_context=ctx,
        )
        assert validation.valid is True, validation.errors

    def test_builder_whitelists_duration_ms_used_by_runtime_narrative(self):
        builder = get_tool_context_builder("SectionSuggestionTool")
        ctx = builder.build(
            graph_state={
                "section_suggestions": {
                    "sections": [{"action": "ai_generate"} for _ in range(17)]
                }
            },
            tool_call_id="SectionSuggestionTool-tc",
            source_event_id="evt-final",
            attempt=1,
            terminal_status="success",
            duration_ms=134,
            continuation_route="section_confirmation",
        )

        assert 134 in ctx.fact_constraints.allowed_numeric_facts
        validation = NarrativeValidator().validate(
            public_update={
                "headline": "Section suggestions ready",
                "summary": "The section list is ready for confirmation.",
                "impact": "The flow is waiting for user confirmation.",
                "next_action": "Continue after the user confirms the sections.",
                "details": [
                    "Generated 17 section suggestions.",
                    "Tool execution took 134 milliseconds.",
                ],
            },
            tool_context=ctx,
        )
        assert validation.valid is True, validation.errors

    def test_word_export_builder_whitelists_artifact_size_and_duration(self):
        builder = get_tool_context_builder("WordExportTool")
        artifact_name = "campus_signin_test_plan.docx"
        ctx = builder.build(
            graph_state={
                "artifact": {
                    "file_name": artifact_name,
                    "file_size": 47670,
                    "public_id": "art_32a90352",
                }
            },
            tool_call_id="WordExportTool-tc",
            source_event_id="evt-final",
            attempt=1,
            terminal_status="success",
            duration_ms=4320,
            continuation_route="check_docx_format",
        )

        assert 47670 in ctx.fact_constraints.allowed_numeric_facts
        assert 4320 in ctx.fact_constraints.allowed_numeric_facts
        assert artifact_name in ctx.fact_constraints.allowed_file_names
        validation = NarrativeValidator().validate(
            public_update={
                "headline": "Word document exported",
                "summary": f"{artifact_name} is ready.",
                "impact": "The generated Word document can enter format checking.",
                "next_action": "Run the docx format check next.",
                "details": [
                    f"Exported artifact is {artifact_name}, size 47670 bytes.",
                    "WordExportTool execution took 4320 milliseconds.",
                ],
            },
            tool_context=ctx,
        )
        assert validation.valid is True, validation.errors

    def test_word_export_builder_whitelists_storage_prefixed_artifact_alias(self):
        builder = get_tool_context_builder("WordExportTool")
        stored_name = "20260803_143926_校园预约签到_测试方案.docx"
        display_name = "校园预约签到_测试方案.docx"
        ctx = builder.build(
            graph_state={
                "artifact": {
                    "file_name": stored_name,
                    "file_size": 48486,
                    "public_id": "art_28aca0c4",
                }
            },
            tool_call_id="WordExportTool-tc",
            source_event_id="evt-final",
            attempt=1,
            terminal_status="success",
            duration_ms=3869,
            continuation_route="check_docx_format",
        )

        assert display_name in ctx.fact_constraints.allowed_file_names
        validation = NarrativeValidator().validate(
            public_update={
                "headline": "测试方案Word文档导出成功",
                "summary": f"已成功生成{display_name}，文件大小48486字节，当前可下载。",
                "impact": "测试方案文档已就绪，可供后续格式校验及交付使用。",
                "next_action": "下一步将执行docx格式检查步骤。",
                "details": [
                    "WordExportTool 执行耗时 3869 毫秒，终端状态为 success。",
                    f"导出产物为 {display_name}，大小 48486 字节。",
                ],
            },
            tool_context=ctx,
        )
        assert validation.valid is True, validation.errors


# ── 4. NarrativeComposer ────────────────────────────────────────────────


class _FakeStreamLLM:
    is_context_engine_bridge = True

    def __init__(self, text):
        self.text = text
        self.calls = 0

    async def stream_with_system(self, system_prompt, user_content, **kw):
        self.calls += 1
        for i in range(0, len(self.text), 4):
            yield self.text[i:i + 4]


class _Sink:
    def __init__(self):
        self.events = []

    async def emit(self, **kw):
        self.events.append(kw)
        return {"ok": True}


def _fallback():
    return AgentPublicUpdateDraft(headline="回退", summary="s", impact="i", next_action="n")


@pytest.mark.asyncio
async def test_composer_success_streams_events_and_completes():
    llm = _FakeStreamLLM(_FULL)
    sink = _Sink()
    composer = NarrativeComposer(
        llm, sink, deterministic_fallback=_fallback(),
        timeout_seconds=10, repair_attempts=0,
    )
    ctx = ToolNarrativeContext(
        tool_name="RequirementParserTool", tool_call_id="tc-1",
        terminal_status="success",
        fact_constraints=NarrativeFactConstraints(allowed_numeric_facts=[43, 7]),
    )
    res = await composer.compose_tool_narrative(
        task_internal_id=90, graph_run_id="run-90",
        context=ctx, narrative_id="nar-1", generation_id="gen-1", generation_no=1,
    )
    assert res.success is True
    assert res.source == "llm"
    assert res.public_update is not None
    assert res.public_update.headline == "需求文档解析已完成"
    assert res.public_update.details == ["识别章节 43 个", "识别表格 7 个"]
    types = [e["event_type"] for e in sink.events]
    assert types[0] == "tool_narrative_started"
    assert "tool_narrative_delta" in types
    assert types[-1] == "tool_narrative_update"
    # 同步顺序: started < delta < update。
    assert types.index("tool_narrative_started") < types.index("tool_narrative_delta")
    assert types.index("tool_narrative_delta") < types.index("tool_narrative_update")


@pytest.mark.asyncio
async def test_composer_falls_back_on_bad_stream():
    bad = "<HEADLINE>未闭合"
    llm = _FakeStreamLLM(bad)
    sink = _Sink()
    composer = NarrativeComposer(
        llm, sink, deterministic_fallback=_fallback(),
        timeout_seconds=10, repair_attempts=0,
    )
    ctx = ToolNarrativeContext(
        tool_name="RequirementParserTool", tool_call_id="tc-1", terminal_status="success",
    )
    res = await composer.compose_tool_narrative(
        task_internal_id=90, graph_run_id="run-90",
        context=ctx, narrative_id="nar-1", generation_id="gen-1", generation_no=1,
    )
    assert res.success is False
    assert res.source == "deterministic"
    assert res.fallback_used is True
    assert res.public_update.headline == "回退"
    types = [e["event_type"] for e in sink.events]
    assert "tool_narrative_failed" in types


@pytest.mark.asyncio
async def test_composer_emits_tool_fallback_event_after_schema_failure():
    bad = (
        "<HEADLINE>Requirement parsed</HEADLINE>"
        "<SUMMARY>Parsed unauthorized_777.docx.</SUMMARY>"
        "<IMPACT>Continue.</IMPACT>"
        "<NEXT_ACTION>Next.</NEXT_ACTION>"
        "<DETAIL>Only one detail.</DETAIL>"
    )
    llm = _FakeStreamLLM(bad)
    sink = _Sink()
    composer = NarrativeComposer(
        llm, sink, deterministic_fallback=_fallback(),
        timeout_seconds=10, repair_attempts=0,
    )
    ctx = ToolNarrativeContext(
        tool_name="RequirementParserTool",
        tool_call_id="tc-1",
        source_event_id="evt-final",
        terminal_status="success",
        fact_constraints=NarrativeFactConstraints(
            allowed_file_names=["allowed.docx"],
            allowed_literal_facts=["allowed.docx"],
            numeric_exempt_literals=["allowed.docx"],
        ),
    )

    res = await composer.compose_tool_narrative(
        task_internal_id=90,
        graph_run_id="run-90",
        context=ctx,
        narrative_id="nar-1",
        generation_id="gen-1",
        generation_no=1,
    )

    assert res.success is False
    assert res.source == "deterministic"
    assert res.fallback_used is True
    types = [e["event_type"] for e in sink.events]
    assert "tool_narrative_failed" in types
    failed_payload = next(
        e["payload"] for e in sink.events if e["event_type"] == "tool_narrative_failed"
    )
    assert failed_payload["failure_category"] == "schema_validation_failed"
    assert failed_payload["validation_errors"]
    assert failed_payload["raw_output_length"] > 0
    assert types[-1] == "tool_narrative_fallback"
    payload = sink.events[-1]["payload"]
    assert payload["source_tool_call_id"] == "tc-1"
    assert payload["source_event_id"] == "evt-final"
    assert payload["narrative_source"] == "deterministic"
    assert payload["fallback_used"] is True
    assert payload["public_update"]["headline"] == res.public_update.headline


@pytest.mark.asyncio
async def test_composer_emits_task_summary_fallback_event_after_schema_failure():
    from app.agent_runtime.narrative_composer.schemas import (
        NarrativeGenerationRequest,
        TaskSummaryNarrativeContext,
    )

    bad = (
        "<HEADLINE>Task completed</HEADLINE>"
        "<SUMMARY>All checks passed.</SUMMARY>"
        "<IMPACT>Ready.</IMPACT>"
        "<NEXT_ACTION>Download.</NEXT_ACTION>"
        "<DETAIL>Only one detail.</DETAIL>"
    )
    llm = _FakeStreamLLM(bad)
    sink = _Sink()
    composer = NarrativeComposer(
        llm, sink, deterministic_fallback=_fallback(),
        timeout_seconds=10, repair_attempts=0,
    )
    request = NarrativeGenerationRequest(
        kind="task_summary",
        narrative_id="tsum-1",
        generation_id="tsumgen-1",
        generation_no=1,
        task_summary_context=TaskSummaryNarrativeContext(
            task_id="task-1",
            graph_run_id="run-90",
            task_status="completed",
            review={"blocking_issues": 3, "warnings": 0, "suggestions": 1},
            fact_constraints=NarrativeFactConstraints(allowed_numeric_facts=[3, 0, 1]),
        ),
        system_prompt="system",
        user_content="user",
    )

    res = await composer._run_generation(
        task_internal_id=90,
        graph_run_id="run-90",
        request=request,
        context_for_fallback=request.task_summary_context,
        event_prefix="task_summary",
        profile=None,
    )

    assert res.success is False
    assert res.source == "deterministic"
    types = [e["event_type"] for e in sink.events]
    assert "task_summary_narrative_failed" in types
    assert types[-1] == "task_summary_narrative_fallback"
    payload = sink.events[-1]["payload"]
    assert payload["narrative_kind"] == "task_summary"
    assert payload["narrative_source"] == "deterministic"
    assert payload["fallback_used"] is True
    assert payload["public_update"]["headline"] == res.public_update.headline


# ── 5. Barrier 同步顺序 + 幂等 ─────────────────────────────────────────


def test_narrative_logical_key_stable():
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_narrative import (
        narrative_logical_key,
    )
    k1 = narrative_logical_key("t", "run", "tc", 1)
    k2 = narrative_logical_key("t", "run", "tc", 1)
    k3 = narrative_logical_key("t", "run", "tc", 2)
    assert k1 == k2
    assert k1 != k3
    assert k1.endswith(":tool_narrative:v1")


@pytest.mark.asyncio
async def test_barrier_disabled_passes_through(monkeypatch):
    """功能关闭时,barrier 直接透传 continuation_route,不调 LLM。"""
    from app.agent_runtime.graphs.test_plan.versions.v3 import nodes_narrative as nn

    async def disabled(_ctx):
        return False

    monkeypatch.setattr(nn, "_tool_narrative_enabled", disabled)
    result = await nn.tool_narrative_barrier_node(
        {"pending_narrative": {"continuation_route": "parse_template", "source_tool_name": "RequirementParserTool", "source_tool_call_id": "tc", "tool_attempt": 1, "terminal_status": "success"}},
        ctx=SimpleNamespace(task_internal_id=90),
    )
    assert result["pending_narrative"] is None
    assert result["next_node"] == "parse_template"


@pytest.mark.asyncio
async def test_barrier_no_llm_client_falls_back(monkeypatch):
    """无 LLMClient → 不阻塞主图,直接释放进入下一节点。"""
    from app.agent_runtime.graphs.test_plan.versions.v3 import nodes_narrative as nn

    async def enabled(_ctx):
        return True

    monkeypatch.setattr(nn, "_tool_narrative_enabled", enabled)
    result = await nn.tool_narrative_barrier_node(
        {"pending_narrative": {"continuation_route": "parse_template", "source_tool_name": "RequirementParserTool", "source_tool_call_id": "tc", "tool_attempt": 1, "terminal_status": "success"}},
        ctx=SimpleNamespace(task_internal_id=90),
    )
    assert result["next_node"] == "parse_template"


@pytest.mark.asyncio
async def test_barrier_idempotent_skip(monkeypatch):
    """已完成叙事(幂等键在 completed 列表) → 直接跳过,不再生成。"""
    from app.agent_runtime.graphs.test_plan.versions.v3 import nodes_narrative as nn

    async def enabled(_ctx):
        return True

    monkeypatch.setattr(nn, "_tool_narrative_enabled", enabled)
    logical_key = nn.narrative_logical_key("t", "run-90", "tc", 1)
    result = await nn.tool_narrative_barrier_node(
        {
            "task_id": "t",
            "graph_run_id": "run-90",
            "pending_narrative": {"continuation_route": "parse_template", "source_tool_name": "RequirementParserTool", "source_tool_call_id": "tc", "tool_attempt": 1, "terminal_status": "success"},
            "narrative_completed_logical_keys": [logical_key],
        },
        ctx=SimpleNamespace(task_internal_id=90),
    )
    assert result["pending_narrative"] is None
    assert result["next_node"] == "parse_template"


@pytest.mark.asyncio
async def test_barrier_user_disabled_skips_llm_before_client_resolution(monkeypatch):
    """The persisted UI preference must stop the LLM call, not merely hide it."""
    from app.agent_runtime.graphs.test_plan.versions.v3 import nodes_narrative as nn

    monkeypatch.setattr(
        nn,
        "get_feature_flags",
        lambda: SimpleNamespace(
            phase29b_tool_narrative_enabled=True,
            phase29b_tool_narrative_blocking_enabled=True,
        ),
    )

    async def disabled_by_user(_ctx):
        return False

    monkeypatch.setattr(
        nn,
        "is_tool_card_narrative_generation_enabled",
        disabled_by_user,
    )

    def llm_must_not_be_resolved(*_args, **_kwargs):
        raise AssertionError("tool-card narrative LLM must not be resolved when disabled")

    monkeypatch.setattr(nn, "_resolve_narrative_llm", llm_must_not_be_resolved)

    result = await nn.tool_narrative_barrier_node(
        {
            "pending_narrative": {
                "continuation_route": "parse_template",
                "source_tool_name": "RequirementParserTool",
                "source_tool_call_id": "tc",
                "tool_attempt": 1,
                "terminal_status": "success",
            }
        },
        ctx=SimpleNamespace(task_internal_id=90, settings_service=None),
    )

    assert result == {
        "pending_narrative": None,
        "next_node": "parse_template",
    }
