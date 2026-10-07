"""Phase 2.9B.5 — Narrative 合同校验与任务状态隔离后端测试。

覆盖:
  - Validator 文件名数字豁免(不会把 file_dc6d128c 的 6/128 判为虚构数字);
  - 未授权普通数字仍被拒绝;
  - Context Builder 使用 original_name / document_name,并登记内部 public_id
    为豁免字面量;
  - Started/Delta/Update/Failed 事件锚点一致(完整锚定字段);
  - Schema Repair 成功 / Repair 失败后确定性 fallback;
  - Task Summary details 2~5 修复;
  - Narrative failed 不修改 Task/Run 状态(barrier 不透传失败为任务失败);
  - 任务完成 + 局部 tool_failed 场景(KnowledgeSearch 降级)任务仍 completed。
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from app.agent_runtime._shared.public_narrative import AgentPublicUpdateDraft
from app.agent_runtime.narrative_composer.composer import NarrativeComposer
from app.agent_runtime.narrative_composer.context_builders import (
    RequirementParserContextBuilder,
    get_tool_context_builder,
)
from app.agent_runtime.narrative_composer.schemas import (
    NarrativeFactConstraints,
    NarrativeGenerationRequest,
    TaskSummaryNarrativeContext,
    ToolNarrativeContext,
)
from app.agent_runtime.narrative_composer.validator import NarrativeValidator

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]


def _ctx(status="success", allowed_nums=None, allowed_files=None, allowed_literals=None, exempt=None):
    return ToolNarrativeContext(
        tool_name="RequirementParserTool",
        tool_call_id="tc",
        terminal_status=status,
        fact_constraints=NarrativeFactConstraints(
            allowed_numeric_facts=allowed_nums or [],
            allowed_file_names=allowed_files or [],
            allowed_literal_facts=allowed_literals or [],
            numeric_exempt_literals=exempt or [],
        ),
    )


# ── 1. Validator 文件名数字豁免 ───────────────────────────────────────────


class TestValidatorFileNumberExemption:
    def test_file_public_id_digits_not_fabricated(self):
        """file_dc6d128c 内嵌的 6/128 不得判为虚构数字(真实 task_92 场景)。"""
        v = NarrativeValidator()
        ctx = _ctx(
            allowed_nums=[43, 7, 3],
            allowed_files=["04_企业报销审批与电子发票管理系统_需求说明书.docx"],
            allowed_literals=["04_企业报销审批与电子发票管理系统_需求说明书.docx"],
            exempt=["file_dc6d128c"],
        )
        res = v.validate(public_update={
            "headline": "需求文档解析完成",
            "summary": "已成功解析文件 file_dc6d128c，提取出43个章节、7个表格及3张图片。",
            "impact": "文档结构化数据已就绪，为后续模板匹配与内容填充提供基础输入。",
            "next_action": "下一步将执行模板解析节点以继续处理流程。",
            "details": [
                "目标文件 file_dc6d128c 解析状态为成功。",
                "共识别出43个章节和7个表格结构。",
                "文档中包含3张图片元素。",
            ],
        }, tool_context=ctx)
        assert res.valid is True, res.errors

    def test_display_name_filename_legal(self):
        """允许字面量 03_IoT设备监控与告警系统需求说明书.docx 合法。"""
        v = NarrativeValidator()
        name = "03_IoT设备监控与告警系统需求说明书.docx"
        ctx = _ctx(
            allowed_nums=[20, 5, 2],
            allowed_files=[name],
            allowed_literals=[name],
            exempt=["file_abc123"],
        )
        res = v.validate(public_update={
            "headline": "解析完成",
            "summary": f"已解析文件 {name}。",
            "impact": "用于确定测试范围。",
            "next_action": "继续。",
            "details": [f"文件 {name} 解析成功", "识别章节 20 个", "识别表格 5 个"],
        }, tool_context=ctx)
        assert res.valid is True, res.errors

    def test_date_prefixed_filename_legal(self):
        """20260802_xxx.docx 作为允许字面量合法(日期数字不得判为虚构)。"""
        v = NarrativeValidator()
        name = "20260802_xxx.docx"
        ctx = _ctx(
            allowed_nums=[3],
            allowed_files=[name],
            allowed_literals=[name],
            exempt=["file_x"],
        )
        res = v.validate(public_update={
            "headline": "H", "summary": "S", "impact": "I", "next_action": "N",
            "details": [f"文件 {name} 解析成功", "识别 3 个模块"],
        }, tool_context=ctx)
        assert res.valid is True, res.errors

    def test_version_literal_legal(self):
        """v3 作为允许字面量合法。"""
        v = NarrativeValidator()
        ctx = _ctx(
            allowed_nums=[8],
            allowed_literals=["v3"],
            exempt=["v3"],
        )
        res = v.validate(public_update={
            "headline": "H", "summary": "S", "impact": "I", "next_action": "N",
            "details": ["图版本 v3 已生成", "生成 8 个章节"],
        }, tool_context=ctx)
        assert res.valid is True, res.errors

    def test_unauthorized_number_still_rejected(self):
        """叙事中独立出现未授权数字 128 仍必须失败。"""
        v = NarrativeValidator()
        ctx = _ctx(allowed_nums=[43, 7, 3])
        res = v.validate(public_update={
            "headline": "H", "summary": "S", "impact": "I", "next_action": "N",
            "details": ["共 128 个章节", "表格 7 个"],
        }, tool_context=ctx)
        assert res.valid is False
        assert any("128" in e for e in res.errors)

    def test_fabricated_99_still_rejected(self):
        """虚构"识别 99 个章节"仍必须失败。"""
        v = NarrativeValidator()
        ctx = _ctx(allowed_nums=[43, 7, 3])
        res = v.validate(public_update={
            "headline": "H", "summary": "S", "impact": "I", "next_action": "N",
            "details": ["识别 99 个章节", "表格 7 个"],
        }, tool_context=ctx)
        assert res.valid is False

    def test_allowed_stat_numbers_pass(self):
        """43 / 7 / 3 允许统计数字必须通过。"""
        v = NarrativeValidator()
        ctx = _ctx(allowed_nums=[43, 7, 3])
        res = v.validate(public_update={
            "headline": "H", "summary": "S", "impact": "I", "next_action": "N",
            "details": ["识别章节 43 个", "识别表格 7 个", "识别图片 3 张"],
        }, tool_context=ctx)
        assert res.valid is True, res.errors

    def test_literal_exemption_not_whole_text_pass(self):
        """allowed_literal_facts 只能按完整值豁免,不能放宽整个文本。"""
        v = NarrativeValidator()
        ctx = _ctx(
            allowed_nums=[43],
            allowed_literals=["file_dc6d128c"],
            exempt=["file_dc6d128c"],
        )
        # 文件名豁免,但独立数字 99 仍失败。
        res = v.validate(public_update={
            "headline": "H", "summary": "S", "impact": "I", "next_action": "N",
            "details": ["处理 file_dc6d128c 完成", "识别 99 个章节"],
        }, tool_context=ctx)
        assert res.valid is False
        assert any("99" in e for e in res.errors)

    def test_unauthorized_filename_rejected(self):
        """未授权文件名(不在白名单)仍失败。"""
        v = NarrativeValidator()
        ctx = _ctx(allowed_nums=[7], allowed_files=["a.docx"], allowed_literals=["a.docx"])
        res = v.validate(public_update={
            "headline": "H", "summary": "S", "impact": "I", "next_action": "N",
            "details": ["解析 secret_plan.docx 完成", "表格 7 个"],
        }, tool_context=ctx)
        assert res.valid is False
        assert any("文件名" in e or "字面量" in e for e in res.errors)

    def test_internal_path_never_leaks(self):
        """不得暴露内部路径/密钥。"""
        v = NarrativeValidator()
        res = v.validate(public_update={
            "headline": "H", "summary": "S", "impact": "I", "next_action": "N",
            "details": ["路径 /home/user/secret/doc.docx", "表格 7 个"],
        })
        assert res.valid is False

    def test_chinese_underscore_hyphen_filename(self):
        """中文/英文/下划线/连字符文件名均覆盖。"""
        v = NarrativeValidator()
        names = [
            "04_企业报销审批与电子发票管理系统_需求说明书.docx",
            "IoT-设备-监控-告警-系统需求说明书.docx",
            "01_PlanWise_QA_测试方案模板.docx",
        ]
        ctx = _ctx(
            allowed_nums=[3],
            allowed_files=names,
            allowed_literals=names,
        )
        for name in names:
            res = v.validate(public_update={
                "headline": "H", "summary": "S", "impact": "I", "next_action": "N",
                "details": [f"文件 {name} 解析成功", "识别 3 个模块"],
            }, tool_context=ctx)
            assert res.valid is True, f"{name}: {res.errors}"


# ── 2. Context Builder 使用 original_name ─────────────────────────────────


class TestContextBuilderOriginalName:
    def test_requirement_builder_uses_document_name(self):
        """Builder 优先使用 document_name(original_name),不把内部 public_id 当展示名。"""
        builder = get_tool_context_builder("RequirementParserTool")
        ctx = builder.build(
            graph_state={
                "requirement_analysis": {
                    "document_structure": [{"name": "s1"}, {"name": "s2"}],
                    "table_summaries": [{"title": "t1"}],
                    "image_texts": [{"text": "img1"}],
                    "document_title": "需求文档",
                    "document_name": "04_企业报销审批与电子发票管理系统_需求说明书.docx",
                    "recognized_image_count": 1,
                },
                "requirement_file_id": "file_dc6d128c",
            },
            tool_call_id="RequirementParserTool-abc",
            source_event_id="evt-1",
            attempt=1,
            terminal_status="success",
            duration_ms=1000,
            continuation_route="parse_template",
        )
        # 展示名使用 original_name,而非 file_dc6d128c。
        assert ctx.input_facts["file_name"] == "04_企业报销审批与电子发票管理系统_需求说明书.docx"
        # 内部 public_id 被登记为豁免字面量。
        assert "file_dc6d128c" in ctx.fact_constraints.numeric_exempt_literals
        # 展示名也在 allowed_literal_facts。
        assert "04_企业报销审批与电子发票管理系统_需求说明书.docx" in ctx.fact_constraints.allowed_literal_facts
        # 统计数字仍在白名单。
        assert 2 in ctx.fact_constraints.allowed_numeric_facts
        assert 1 in ctx.fact_constraints.allowed_numeric_facts

    def test_requirement_builder_falls_back_to_internal_id(self):
        """无 document_name 时回退到内部 public_id,但仍登记为豁免字面量。"""
        builder = get_tool_context_builder("RequirementParserTool")
        ctx = builder.build(
            graph_state={
                "requirement_analysis": {"document_structure": [{"name": "s1"}]},
                "requirement_file_id": "file_dc6d128c",
            },
            tool_call_id="t", source_event_id="e", attempt=1,
            terminal_status="success", duration_ms=1,
            continuation_route="next",
        )
        assert ctx.input_facts["file_name"] == "file_dc6d128c"
        assert "file_dc6d128c" in ctx.fact_constraints.numeric_exempt_literals


# ── 3. 事件锚点一致 ───────────────────────────────────────────────────────


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


def _full_pu():
    return (
        "<HEADLINE>需求文档解析完成</HEADLINE>"
        "<SUMMARY>识别43个章节、7个表格。</SUMMARY>"
        "<IMPACT>用于确定测试范围。</IMPACT>"
        "<NEXT_ACTION>接下来解析模板。</NEXT_ACTION>"
        "<DETAIL>识别章节 43 个</DETAIL>"
        "<DETAIL>识别表格 7 个</DETAIL>"
    )


ANCHOR_KEYS = [
    "narrative_id",
    "generation_id",
    "generation_no",
    "source_event_id",
    "source_tool_call_id",
    "tool_name",
    "attempt",
    "schema_version",
    "narrative_source",
    "narrative_kind",
]


@pytest.mark.asyncio
async def test_all_narrative_events_share_consistent_anchor():
    """started / delta / update / failed 使用同一套锚定字段。"""
    llm = _FakeStreamLLM(_full_pu())
    sink = _Sink()
    composer = NarrativeComposer(llm, sink, timeout_seconds=10, repair_attempts=0)
    ctx = ToolNarrativeContext(
        tool_name="RequirementParserTool", tool_call_id="tc-abc",
        source_event_id="evt-9", attempt=2, terminal_status="success",
        fact_constraints=NarrativeFactConstraints(allowed_numeric_facts=[43, 7]),
    )
    res = await composer.compose_tool_narrative(
        task_internal_id=90, graph_run_id="run-90",
        context=ctx, narrative_id="nar-1", generation_id="gen-1", generation_no=3,
    )
    assert res.success is True
    for evt in sink.events:
        payload = evt["payload"]
        for key in ANCHOR_KEYS:
            assert key in payload, f"{evt['event_type']} 缺 {key}"
        # 全部事件锚定到同一 narrative / generation / source_tool_call_id / attempt。
        assert payload["narrative_id"] == "nar-1"
        assert payload["generation_id"] == "gen-1"
        assert payload["generation_no"] == 3
        assert payload["source_tool_call_id"] == "tc-abc"
        assert payload["tool_name"] == "RequirementParserTool"
        assert payload["attempt"] == 2
        assert payload["source_event_id"] == "evt-9"


@pytest.mark.asyncio
async def test_started_anchors_to_tool_card():
    """started payload 必须带 source_tool_call_id,前端可锚定到 Tool 卡片。"""
    llm = _FakeStreamLLM(_full_pu())
    sink = _Sink()
    composer = NarrativeComposer(llm, sink, timeout_seconds=10, repair_attempts=0)
    ctx = ToolNarrativeContext(
        tool_name="RequirementParserTool", tool_call_id="RequirementParserTool-task_05d8a139",
        source_event_id="evt-9", attempt=1, terminal_status="success",
        fact_constraints=NarrativeFactConstraints(allowed_numeric_facts=[43, 7]),
    )
    await composer.compose_tool_narrative(
        task_internal_id=90, graph_run_id="run-90",
        context=ctx, narrative_id="nar-1", generation_id="gen-1", generation_no=1,
    )
    started = next(e for e in sink.events if e["event_type"] == "tool_narrative_started")
    assert started["payload"]["source_tool_call_id"] == "RequirementParserTool-task_05d8a139"


# ── 4. Schema Repair 成功 / 失败后 fallback ───────────────────────────────


class _RepairLLM:
    is_context_engine_bridge = True

    def __init__(self, first, repair):
        self.first, self.repair = first, repair
        self.calls = 0

    async def stream_with_system(self, system_prompt, user_content, **kw):
        self.calls += 1
        yield (self.first if self.calls == 1 else self.repair)


@pytest.mark.asyncio
async def test_repair_success_after_schema_failure():
    """首次 schema 失败(1 DETAIL)、Repair 成功(2 DETAIL)→ update。"""
    first = (
        "<HEADLINE>需求文档解析完成</HEADLINE><SUMMARY>识别43个章节。</SUMMARY>"
        "<IMPACT>用于确定测试范围。</IMPACT><NEXT_ACTION>接下来解析模板。</NEXT_ACTION>"
        "<DETAIL>识别章节 43 个</DETAIL>"
    )
    repair = (
        "<HEADLINE>需求文档解析完成</HEADLINE><SUMMARY>识别43个章节、7个表格。</SUMMARY>"
        "<IMPACT>用于确定测试范围。</IMPACT><NEXT_ACTION>接下来解析模板。</NEXT_ACTION>"
        "<DETAIL>识别章节 43 个</DETAIL><DETAIL>识别表格 7 个</DETAIL>"
    )
    llm = _RepairLLM(first, repair)
    sink = _Sink()
    composer = NarrativeComposer(
        llm, sink, timeout_seconds=10, repair_attempts=1,
    )
    ctx = ToolNarrativeContext(
        tool_name="RequirementParserTool", tool_call_id="tc-1", terminal_status="success",
        fact_constraints=NarrativeFactConstraints(allowed_numeric_facts=[43, 7]),
    )
    res = await composer.compose_tool_narrative(
        task_internal_id=90, graph_run_id="run-90",
        context=ctx, narrative_id="nar-1", generation_id="gen-1", generation_no=1,
    )
    assert res.success is True
    assert res.source == "llm"
    assert len(res.public_update.details) == 2
    assert llm.calls == 2
    types = [e["event_type"] for e in sink.events]
    assert types[-1] == "tool_narrative_update"


@pytest.mark.asyncio
async def test_repair_fails_then_deterministic_fallback():
    """首次失败、Repair 仍失败 → 确定性 fallback 生效。"""
    bad = (
        "<HEADLINE>H</HEADLINE><SUMMARY>S</SUMMARY><IMPACT>I</IMPACT><NEXT_ACTION>N</NEXT_ACTION>"
        "<DETAIL>识别 999 个章节</DETAIL><DETAIL>表格 7 个</DETAIL>"
    )
    llm = _RepairLLM(bad, bad)
    sink = _Sink()
    fallback = AgentPublicUpdateDraft(headline="回退", summary="s", impact="i", next_action="n")
    composer = NarrativeComposer(
        llm, sink, deterministic_fallback=fallback, timeout_seconds=10, repair_attempts=1,
    )
    ctx = ToolNarrativeContext(
        tool_name="RequirementParserTool", tool_call_id="tc-1", terminal_status="success",
        fact_constraints=NarrativeFactConstraints(allowed_numeric_facts=[43, 7]),
    )
    res = await composer.compose_tool_narrative(
        task_internal_id=90, graph_run_id="run-90",
        context=ctx, narrative_id="nar-1", generation_id="gen-1", generation_no=1,
    )
    assert res.success is False
    assert res.source == "deterministic"
    assert res.fallback_used is True
    assert res.public_update.headline == "回退"
    assert llm.calls == 2
    types = [e["event_type"] for e in sink.events]
    assert "tool_narrative_failed" in types
    assert "tool_narrative_update" not in types


# ── 5. Task Summary details 2~5 修复 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_task_summary_one_detail_triggers_repair():
    """Task Summary 只有 1 DETAIL → Repair 补足 2 条 → update。"""
    first = (
        "<HEADLINE>报销审批测试方案生成完成</HEADLINE><SUMMARY>已生成测试方案。</SUMMARY>"
        "<IMPACT>提供标准化文档。</IMPACT><NEXT_ACTION>产物可下载。</NEXT_ACTION>"
        "<DETAIL>任务全流程8个工具调用均成功。</DETAIL>"
    )
    repair = (
        "<HEADLINE>报销审批测试方案生成完成</HEADLINE><SUMMARY>已生成测试方案。</SUMMARY>"
        "<IMPACT>提供标准化文档。</IMPACT><NEXT_ACTION>产物可下载。</NEXT_ACTION>"
        "<DETAIL>任务全流程8个工具调用均成功。</DETAIL><DETAIL>生成的文档为docx格式。</DETAIL>"
    )
    llm = _RepairLLM(first, repair)
    sink = _Sink()
    composer = NarrativeComposer(llm, sink, timeout_seconds=10, repair_attempts=1)
    ctx = TaskSummaryNarrativeContext(
        task_id="task_05d8a139", task_status="completed", task_goal="g",
        generated_sections=8, artifact={"name": "报销审批_测试方案.docx", "available": True},
        fact_constraints=NarrativeFactConstraints(allowed_numeric_facts=[8]),
    )
    from app.agent_runtime.narrative_composer.prompts import build_task_summary_prompt
    sp, uc = build_task_summary_prompt(ctx)
    request = NarrativeGenerationRequest(
        kind="task_summary", narrative_id="tsum-1", generation_id="tgen-1", generation_no=1,
        task_summary_context=ctx, system_prompt=sp, user_content=uc,
    )
    res = await composer._run_generation(
        task_internal_id=90, graph_run_id="run-90", request=request,
        context_for_fallback=ctx, event_prefix="task_summary", profile=None,
    )
    assert res.success is True
    assert len(res.public_update.details) == 2
    types = [e["event_type"] for e in sink.events]
    assert types[0] == "task_summary_narrative_started"
    assert types[-1] == "task_summary_narrative_update"
    # 不串用 tool_narrative 事件。
    assert not any(t.startswith("tool_narrative_") for t in types)


@pytest.mark.asyncio
async def test_task_summary_repair_still_one_detail_falls_back():
    """Repair 仍只有 1 DETAIL → deterministic fallback,任务仍 completed 语义。"""
    one = (
        "<HEADLINE>H</HEADLINE><SUMMARY>S</SUMMARY><IMPACT>I</IMPACT><NEXT_ACTION>N</NEXT_ACTION>"
        "<DETAIL>只有一条事实</DETAIL>"
    )
    llm = _RepairLLM(one, one)
    sink = _Sink()
    fallback = AgentPublicUpdateDraft(headline="任务已完成", summary="s", impact="i", next_action="n")
    composer = NarrativeComposer(
        llm, sink, deterministic_fallback=fallback, timeout_seconds=10, repair_attempts=1,
    )
    ctx = TaskSummaryNarrativeContext(
        task_id="t", task_status="completed", task_goal="g",
        fact_constraints=NarrativeFactConstraints(),
    )
    from app.agent_runtime.narrative_composer.prompts import build_task_summary_prompt
    sp, uc = build_task_summary_prompt(ctx)
    request = NarrativeGenerationRequest(
        kind="task_summary", narrative_id="tsum-1", generation_id="tgen-1", generation_no=1,
        task_summary_context=ctx, system_prompt=sp, user_content=uc,
    )
    res = await composer._run_generation(
        task_internal_id=90, graph_run_id="run-90", request=request,
        context_for_fallback=ctx, event_prefix="task_summary", profile=None,
    )
    assert res.success is False
    assert res.source == "deterministic"
    types = [e["event_type"] for e in sink.events]
    assert "task_summary_narrative_failed" in types


@pytest.mark.asyncio
async def test_task_summary_decode_error_retries_with_raw_output_and_uses_llm_result():
    """A bare initial response must enter contract repair before deterministic fallback."""
    first = "### 任务概览\n模型返回了正常的总结正文，但遗漏了 <NARRATIVE> 包装标签。"
    repair = (
        "<NARRATIVE>### 任务概览\n"
        "测试方案已生成并导出。\n\n"
        "### 审查与待确认\n"
        "- 当前审查未发现阻断交付的问题。\n"
        "</NARRATIVE>"
    )
    llm = _RepairLLM(first, repair)
    sink = _Sink()
    composer = NarrativeComposer(llm, sink, timeout_seconds=10, repair_attempts=1)
    ctx = TaskSummaryNarrativeContext(
        task_id="task-decode-repair",
        task_status="completed",
        task_goal="生成测试方案",
        fact_constraints=NarrativeFactConstraints(),
    )
    from app.agent_runtime.narrative_composer.prompts import build_task_summary_prompt

    system_prompt, user_content = build_task_summary_prompt(ctx)
    request = NarrativeGenerationRequest(
        kind="task_summary",
        narrative_id="tsum-decode-repair",
        generation_id="tgen-decode-repair",
        generation_no=1,
        task_summary_context=ctx,
        system_prompt=system_prompt,
        user_content=user_content,
    )

    result = await composer._run_generation(
        task_internal_id=90,
        graph_run_id="run-90",
        request=request,
        context_for_fallback=ctx,
        event_prefix="task_summary",
        profile=None,
    )

    assert result.success is True
    assert result.source == "llm"
    assert "任务概览" in result.public_update.narrative_text
    assert llm.calls == 2
    event_types = [event["event_type"] for event in sink.events]
    assert "task_summary_narrative_failed" not in event_types
    assert event_types[-1] == "task_summary_narrative_update"


# ── 6. Narrative failed 不修改 Task/Run 状态 ──────────────────────────────


@pytest.mark.asyncio
async def test_tool_narrative_failed_does_not_fail_task(monkeypatch):
    """barrier 返回 narrative_result_summary 时任务状态不被改写为失败。"""
    from app.agent_runtime.graphs.test_plan.versions.v3 import nodes_narrative as nn

    monkeypatch.setattr(nn, "_tool_narrative_enabled", lambda: True)
    # 无 llm_client → 确定性回退,不透传失败为任务失败。
    result = await nn.tool_narrative_barrier_node(
        {"pending_narrative": {
            "continuation_route": "parse_template",
            "source_tool_name": "RequirementParserTool",
            "source_tool_call_id": "tc",
            "source_event_id": "evt-1",
            "tool_attempt": 1,
            "terminal_status": "success",
        }},
        ctx=type("Ctx", (), {"task_internal_id": 90})(),
    )
    assert result["next_node"] == "parse_template"
    assert "task_status" not in result
    assert "last_error" not in result


@pytest.mark.asyncio
async def test_barrier_result_keeps_task_status_untouched(monkeypatch):
    """叙事 success/failed 只记录 summary,不触碰任务终态。"""
    from app.agent_runtime.graphs.test_plan.versions.v3 import nodes_narrative as nn

    monkeypatch.setattr(nn, "_tool_narrative_enabled", lambda: True)
    result = await nn.tool_narrative_barrier_node(
        {"task_id": "t", "graph_run_id": "run-90",
         "pending_narrative": {
             "continuation_route": "parse_template",
             "source_tool_name": "RequirementParserTool",
             "source_tool_call_id": "tc",
             "tool_attempt": 1,
             "terminal_status": "success",
         }},
        ctx=type("Ctx", (), {"task_internal_id": 90})(),
    )
    # 无 llm_client 时只释放,不写任何任务失败字段。
    assert result["pending_narrative"] is None
    assert "task_status" not in result


@pytest.mark.asyncio
async def test_barrier_composer_error_never_fails_task(monkeypatch):
    """compose_tool_narrative 抛异常时 barrier 走确定性回退,任务不被失败。"""
    from app.agent_runtime.graphs.test_plan.versions.v3 import nodes_narrative as nn
    from app.agent_runtime.narrative_composer import context_builders as cb

    monkeypatch.setattr(nn, "_tool_narrative_enabled", lambda: True)
    # Exercise the builder failure through the CE-only path rather than
    # returning early because the retired legacy client is absent.
    monkeypatch.setattr(nn, "_resolve_narrative_llm", lambda *_args, **_kwargs: object())

    class _ExplodingBuilder:
        def build(self, **kw):
            raise RuntimeError("builder boom")

    monkeypatch.setattr(cb, "get_tool_context_builder", lambda name: _ExplodingBuilder())

    class _Ctx:
        task_internal_id = 90
        llm_client = object()
        event_sink = _Sink()

    result = await nn.tool_narrative_barrier_node(
        {"task_id": "t", "graph_run_id": "run-90",
         "pending_narrative": {
             "continuation_route": "parse_template",
             "source_tool_name": "RequirementParserTool",
             "source_tool_call_id": "tc",
             "tool_attempt": 1,
             "terminal_status": "success",
         }},
        ctx=_Ctx(),
    )
    # 节点不抛异常、任务不被失败,只走确定性回退。
    assert result["next_node"] == "parse_template"
    assert "task_status" not in result
    assert result["narrative_result_summary"]["fallback_used"] is True
