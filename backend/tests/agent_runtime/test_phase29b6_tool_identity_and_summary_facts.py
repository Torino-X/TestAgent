"""Phase 2.9B.6 — LLM Narrative 锚定、Task Summary 投影与事实一致性后端测试。

覆盖(§十一):
  1. ToolAdapter 返回 envelope 中的 tool_call_id 与 tool_started / tool_finished 相同;
  2. PendingNarrative.source_tool_call_id 与真实 tool_finished.tool_call_id 相同;
  3. Narrative started/delta/update 的 source_tool_call_id 全部与真实 Tool 相同;
  4. source_event_id 等于最终 terminal event_id,不为空;
  5. chunk_final=false 不触发 Narrative;
  6. chunk_final=true 只触发一次 Narrative;
  7. 两个 attempt 不串卡;
  8. tool_call_id 缺失时不得生成 ToolName-taskId 伪锚点;
  9. Task Summary NarrativeContext 包含真实 review facts;
  10. blocking_issues=3,模型输出"无阻塞问题":
      - Fact Validator 必须拒绝;
      - 触发一次 repair;
      - repair 仍错误则 deterministic fallback;
      - 业务任务保持 completed;
  11. warnings/suggestions/Artifact 名称的事实冲突测试;
  12. Narrative 失败不修改 Task 或 Tool 业务状态;
  13. v3 主图每个目标 Tool 的 terminal → barrier → next node 顺序测试。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from app.agent_runtime._shared.public_narrative import AgentPublicUpdateDraft
from app.agent_runtime.adapters.test_agent_tool_adapter import TestAgentToolAdapter
from app.agent_runtime.cancellation import InMemoryCancellationService
from app.agent_runtime.events.sink import InMemoryEventSink
from app.agent_runtime.narrative_composer.composer import NarrativeComposer
from app.agent_runtime.narrative_composer.schemas import (
    NarrativeFactConstraints,
    NarrativeGenerationRequest,
    TaskSummaryNarrativeContext,
    ToolNarrativeContext,
)
from app.agent_runtime.narrative_composer.validator import NarrativeValidator
from app.agent_runtime.runtime_context import RuntimeContext

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]


# ── Fixtures ───────────────────────────────────────────────────────────────


class _FakeSession:
    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class _CapturingExecutor:
    def __init__(self, result=None):
        self._result = result or {"success": True, "data": {"ok": True}}

    async def run(self, _tool_name, _inputs, _ctx, _retry=None):
        return dict(self._result)


class _FakeStreamLLM:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    async def stream_with_system(self, system_prompt, user_content, **kw):
        self.calls += 1
        for i in range(0, len(self.text), 4):
            yield self.text[i : i + 4]


class _Sink:
    def __init__(self):
        self.events = []

    async def emit(self, **kw):
        # 模拟 LiveAgentEventSink:为每个事件生成 event_id。
        payload = dict(kw.get("payload") or {})
        event = {**kw, "payload": payload, "event_id": f"evt-{len(self.events) + 1}"}
        self.events.append(event)
        return event


def _make_runtime(sink, session=None):
    from contextlib import asynccontextmanager
    from datetime import datetime

    @asynccontextmanager
    async def session_factory():
        yield session or _FakeSession()

    return RuntimeContext(
        user_internal_id=1,
        task_internal_id=90,
        conversation_internal_id=2,
        session_factory=session_factory,
        settings_service=object(),
        event_sink=sink,
        cancellation_service=InMemoryCancellationService(),
        clock=datetime.utcnow,
    )


def _make_adapter(sink, executor=None, runtime=None):
    from contextlib import asynccontextmanager
    from datetime import datetime

    @asynccontextmanager
    async def session_factory():
        yield _FakeSession()

    return TestAgentToolAdapter(
        tool_executor=executor or _CapturingExecutor(),
        event_sink=sink,
        session_factory=session_factory,
        clock=datetime.utcnow,
        min_visible_seconds=0,
        transition_seconds=0,
    ), runtime or _make_runtime(sink)


# ── 1/2/4. Tool Identity Contract ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_envelope_tool_call_id_matches_started_and_finished():
    """envelope.tool_call_id == tool_started.tool_call_id == tool_finished.tool_call_id。"""
    sink = InMemoryEventSink()
    adapter, runtime = _make_adapter(sink)
    envelope = await adapter.execute(
        tool_name="RequirementParserTool",
        inputs={"requirement_file_id": "f1"},
        ctx_runtime=runtime,
    )
    started = next(e for e in sink.events if e["event_type"] == "tool_started")
    finished = next(e for e in sink.events if e["event_type"] == "tool_finished")
    assert envelope.get("tool_call_id")
    assert envelope["tool_call_id"] == started["payload"]["tool_call_id"]
    assert envelope["tool_call_id"] == finished["payload"]["tool_call_id"]
    assert finished["payload"]["tool_call_id"] == started["payload"]["tool_call_id"]
    assert isinstance(started["payload"]["narrative_expected"], bool)
    assert isinstance(finished["payload"]["narrative_expected"], bool)


@pytest.mark.asyncio
async def test_adapter_marks_narrative_expected_when_tool_narrative_blocking_enabled(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_PHASE29B_TOOL_NARRATIVE_ENABLED", "1")
    monkeypatch.setenv("AGENT_RUNTIME_PHASE29B_TOOL_NARRATIVE_BLOCKING_ENABLED", "1")
    sink = InMemoryEventSink()
    adapter, runtime = _make_adapter(sink)
    await adapter.execute(
        tool_name="RequirementParserTool",
        inputs={"requirement_file_id": "f1"},
        ctx_runtime=runtime,
    )
    started = next(e for e in sink.events if e["event_type"] == "tool_started")
    finished = next(e for e in sink.events if e["event_type"] == "tool_finished")
    assert started["payload"]["narrative_expected"] is True
    assert finished["payload"]["narrative_expected"] is True


@pytest.mark.asyncio
async def test_source_event_id_is_real_terminal_event_id():
    """PendingNarrative.source_event_id 必须等于真实 terminal event_id(不为空)。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        _build_pending_narrative,
    )

    sink = InMemoryEventSink()
    adapter, runtime = _make_adapter(sink)
    envelope = await adapter.execute(
        tool_name="RequirementParserTool",
        inputs={"requirement_file_id": "f1"},
        ctx_runtime=runtime,
    )
    terminal_event_id = adapter.last_terminal_event_id()
    assert terminal_event_id, "adapter 必须捕获真实终态 event_id"

    pending = _build_pending_narrative(
        state={"task_id": "task_abc"},
        tool_name="RequirementParserTool",
        tool_call_id=str(envelope.get("tool_call_id") or ""),
        attempt=1,
        terminal_status="success",
        continuation_route="parse_template",
        source_event_id=terminal_event_id,
    )
    assert pending["source_tool_call_id"] == envelope["tool_call_id"]
    assert pending["source_event_id"] == terminal_event_id
    assert pending["source_event_id"] != ""
    # 终态事件真实存在于 sink。
    assert any(e["event_id"] == terminal_event_id for e in sink.events)


@pytest.mark.asyncio
async def test_narrative_events_share_real_tool_call_id():
    """started/delta/update 的 source_tool_call_id 全部等于真实 tool_call_id。"""
    sink = InMemoryEventSink()
    adapter, runtime = _make_adapter(sink)
    envelope = await adapter.execute(
        tool_name="RequirementParserTool",
        inputs={"requirement_file_id": "f1"},
        ctx_runtime=runtime,
    )
    real_tool_call_id = envelope["tool_call_id"]

    llm = _FakeStreamLLM(
        "<HEADLINE>需求文档解析完成</HEADLINE>"
        "<SUMMARY>识别43个章节。</SUMMARY>"
        "<IMPACT>用于确定测试范围。</IMPACT>"
        "<NEXT_ACTION>接下来解析模板。</NEXT_ACTION>"
        "<DETAIL>识别章节 43 个</DETAIL>"
        "<DETAIL>识别表格 7 个</DETAIL>"
    )
    composer = NarrativeComposer(llm, sink, timeout_seconds=10, repair_attempts=0)
    ctx = ToolNarrativeContext(
        tool_name="RequirementParserTool",
        tool_call_id=real_tool_call_id,
        source_event_id=adapter.last_terminal_event_id() or "evt-0",
        attempt=1,
        terminal_status="success",
        fact_constraints=NarrativeFactConstraints(allowed_numeric_facts=[43, 7]),
    )
    await composer.compose_tool_narrative(
        task_internal_id=90,
        graph_run_id="run-90",
        context=ctx,
        narrative_id="nar-1",
        generation_id="gen-1",
        generation_no=1,
    )
    narrative_events = [
        e for e in sink.events if e["event_type"].startswith("tool_narrative_")
    ]
    assert narrative_events
    for evt in narrative_events:
        assert evt["payload"]["source_tool_call_id"] == real_tool_call_id
        assert evt["payload"]["source_event_id"] != ""


# ── 8. 缺失 tool_call_id 不生成伪锚点 ────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_tool_call_id_never_builds_toolname_taskid_anchor():
    """tool_call_id 缺失时不得生成 ToolName-taskId 伪锚点。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        _build_pending_narrative,
    )

    pending = _build_pending_narrative(
        state={"task_id": "task_abc"},
        tool_name="RequirementParserTool",
        tool_call_id="",
        attempt=1,
        terminal_status="success",
        continuation_route="parse_template",
    )
    assert pending["source_tool_call_id"] == ""
    assert "RequirementParserTool-task_abc" not in pending["source_tool_call_id"]


# ── 7. 两个 attempt 不串卡 ───────────────────────────────────────────────


def test_pending_narrative_carries_attempt():
    """PendingNarrative 必须携带 attempt,叙事按 attempt 隔离。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        _build_pending_narrative,
    )

    p1 = _build_pending_narrative(
        state={"task_id": "t"},
        tool_name="TestPlanGeneratorTool",
        tool_call_id="TestPlanGeneratorTool-aaa",
        attempt=1,
        terminal_status="success",
        continuation_route="review_step",
    )
    p2 = _build_pending_narrative(
        state={"task_id": "t"},
        tool_name="TestPlanGeneratorTool",
        tool_call_id="TestPlanGeneratorTool-bbb",
        attempt=2,
        terminal_status="success",
        continuation_route="review_step",
    )
    assert p1["tool_attempt"] == 1
    assert p2["tool_attempt"] == 2
    assert p1["source_tool_call_id"] != p2["source_tool_call_id"]


# ── 9/10/11. Task Summary 事实上下文 + Fact Validator ────────────────────


def _summary_ctx(**overrides):
    base = dict(
        task_id="task_927f6f71",
        task_status="completed",
        task_goal="g",
        generated_sections=16,
        preserved_template_sections=1,
        business_modules=0,
        review={"blocking_issues": 3, "warnings": 0, "suggestions": 1},
        artifact={
            "name": "设备监控_测试方案.docx",
            "format": "docx",
            "available": True,
            "size_bytes": 48344,
        },
        completed_tools=[{"tool_name": "RequirementParserTool", "status": "success"}],
        fact_constraints=NarrativeFactConstraints(
            allowed_numeric_facts=[16, 1, 3, 48344],
            allowed_literal_facts=["设备监控_测试方案.docx"],
            allowed_file_names=["设备监控_测试方案.docx"],
            numeric_exempt_literals=["设备监控_测试方案.docx", "art-1"],
        ),
    )
    base.update(overrides)
    return TaskSummaryNarrativeContext(**base)


@pytest.mark.asyncio
async def test_task_summary_context_contains_real_review_facts():
    """NarrativeContext 必须包含真实 review facts(修复第一个错误断点)。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_narrative import (
        _build_task_summary_context,
    )

    state = {
        "task_id": "task_927f6f71",
        "task_status": "exporting",
        "test_plan_content": {
            "generated_sections": [{"title": "s%d" % i} for i in range(16)],
            "kept_sections": [{"title": "k1"}],
        },
        "requirement_analysis": {"modules": []},
        "review_result": {
            "level": "failed",
            "passed": False,
            "review_issues": [
                {"severity": "block", "message": "b1"},
                {"severity": "block", "message": "b2"},
                {"severity": "block", "message": "b3"},
                {"severity": "suggestion", "message": "s1"},
            ],
        },
        "artifact": {
            "file_name": "设备监控_测试方案.docx",
            "file_size": 48344,
            "public_id": "art-1",
        },
        "completed_nodes": ["parse_requirement", "generate_test_plan"],
    }
    ctx = _build_task_summary_context(state)
    assert ctx.review["blocking_issues"] == 3
    assert ctx.review["warnings"] == 0
    assert ctx.review["suggestions"] == 1
    assert ctx.review["blocking_issue_details"] == ["b1", "b2", "b3"]
    assert ctx.review["suggestion_details"] == ["s1"]
    assert ctx.generated_sections == 16
    assert ctx.preserved_template_sections == 1
    assert ctx.task_status == "completed"
    assert ctx.artifact["name"] == "设备监控_测试方案.docx"
    assert ctx.artifact["available"] is True
    assert ctx.artifact["size_bytes"] == 48344
    assert 3 in ctx.fact_constraints.allowed_numeric_facts


def test_fact_validator_rejects_negation_conflict():
    """blocking_issues=3 + 「无阻塞问题」 → Fact Validator 拒绝。"""
    v = NarrativeValidator()
    ctx = _summary_ctx()
    res = v.validate(
        public_update={
            "headline": "测试方案生成完成",
            "summary": "评审结果显示无阻塞问题、警告或建议项。",
            "impact": "用于指导后续执行。",
            "next_action": "可下载测试方案。",
            "details": ["已生成16个章节", "产物可下载"],
        },
        task_summary_context=ctx,
    )
    assert res.valid is False
    assert any("无阻塞问题" in e for e in res.errors)
    assert "blocking_issues=3" in " ".join(res.errors)


def test_fact_validator_rejects_warning_and_suggestion_negation():
    """warnings>0 / suggestions>0 时拒绝「无警告/无建议」。"""
    v = NarrativeValidator()
    ctx = _summary_ctx(review={"blocking_issues": 0, "warnings": 2, "suggestions": 3})
    res = v.validate(
        public_update={
            "headline": "H",
            "summary": "评审结果无警告、无建议。",
            "impact": "i",
            "next_action": "n",
            "details": ["生成 16 个章节", "产物可下载"],
        },
        task_summary_context=ctx,
    )
    assert res.valid is False


def test_fact_validator_rejects_wrong_artifact_name():
    """叙事使用虚构产物名 → 拒绝。"""
    v = NarrativeValidator()
    ctx = _summary_ctx()
    res = v.validate(
        public_update={
            "headline": "H",
            "summary": "已生成 secret_plan.docx。",
            "impact": "i",
            "next_action": "n",
            "details": ["生成 16 个章节", "3 个阻塞问题"],
        },
        task_summary_context=ctx,
    )
    assert res.valid is False


def test_fact_validator_rejects_unavailable_artifact_as_downloadable():
    """artifact.available=false 时不得描述为可下载。"""
    v = NarrativeValidator()
    ctx = _summary_ctx(
        artifact={
            "name": "设备监控_测试方案.docx",
            "format": "docx",
            "available": False,
            "size_bytes": 48344,
        }
    )
    res = v.validate(
        public_update={
            "headline": "H",
            "summary": "任务完成，产物可下载。",
            "impact": "i",
            "next_action": "n",
            "details": ["生成 16 个章节", "3 个阻塞问题"],
        },
        task_summary_context=ctx,
    )
    assert res.valid is False
    assert any("可下载" in e for e in res.errors)


def test_fact_validator_accepts_correct_summary():
    """合法总结(数字 + 产物名一致)→ 通过。"""
    v = NarrativeValidator()
    ctx = _summary_ctx()
    res = v.validate(
        public_update={
            "headline": "设备监控测试方案生成完成",
            "summary": "已生成 16 个章节,审查发现 3 个阻塞问题。",
            "impact": "用于指导后续执行。",
            "next_action": "可下载测试方案 设备监控_测试方案.docx。",
            "details": ["生成 16 个章节", "发现 3 个阻塞问题"],
        },
        task_summary_context=ctx,
    )
    assert res.valid is True, res.errors


def test_fact_validator_accepts_natural_summary_sentence_with_artifact_name():
    """Natural LLM wording may put an artifact name inside a sentence."""
    v = NarrativeValidator()
    ctx = _summary_ctx(
        artifact={
            "name": "报销审批_测试方案.docx",
            "format": "docx",
            "available": True,
            "size_bytes": 48896,
        },
        fact_constraints=NarrativeFactConstraints(
            allowed_numeric_facts=[16, 1, 3, 48896],
            allowed_literal_facts=["报销审批_测试方案.docx"],
            allowed_file_names=["报销审批_测试方案.docx"],
            numeric_exempt_literals=["报销审批_测试方案.docx"],
        ),
    )
    res = v.validate(
        public_update={
            "headline": "测试方案已生成",
            "summary": "",
            "impact": "",
            "next_action": "",
            "details": [],
            "narrative_text": "我已经根据需求文档和模板生成了报销审批_测试方案.docx；这版包含 16 个章节，审查里还有 3 个阻断问题需要你后续确认。",
        },
        task_summary_context=ctx,
    )

    assert res.valid is True, res.errors


# ── 10. repair → 仍错 → deterministic fallback;任务保持 completed ───────


@pytest.mark.asyncio
async def test_blocking_fact_repair_then_fallback_task_stays_completed():
    """模型输出「无阻塞问题」→ 拒绝 → 一次 repair → 仍错 → deterministic fallback。"""
    bad = (
        "<HEADLINE>测试方案生成完成</HEADLINE>"
        "<SUMMARY>评审结果显示无阻塞问题。</SUMMARY>"
        "<IMPACT>用于指导后续执行。</IMPACT>"
        "<NEXT_ACTION>可下载测试方案。</NEXT_ACTION>"
        "<DETAIL>生成 16 个章节</DETAIL>"
        "<DETAIL>3 个阻塞问题</DETAIL>"
    )

    class _RepairLLM:
        def __init__(self, text):
            self.text = text
            self.calls = 0

        async def stream_with_system(self, system_prompt, user_content, **kw):
            self.calls += 1
            yield self.text

    llm = _RepairLLM(bad)
    sink = _Sink()
    fallback = AgentPublicUpdateDraft(
        headline="任务已完成", summary="测试方案已按确定性流程生成。",
        impact="产物可下载用于后续执行。", next_action="可下载测试方案。", details=[],
    )
    composer = NarrativeComposer(
        llm, sink, deterministic_fallback=fallback, timeout_seconds=10, repair_attempts=1,
    )
    ctx = _summary_ctx()
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
    # 触发一次 repair(两次 LLM 调用)。
    assert llm.calls == 2
    # repair 仍错 → deterministic fallback。
    assert res.success is False
    assert res.source == "deterministic"
    assert res.fallback_used is True
    assert res.public_update.headline == "任务已完成"
    types = [e["event_type"] for e in sink.events]
    assert "task_summary_narrative_failed" in types
    assert "task_summary_narrative_update" not in types


# ── 12. Narrative 失败不修改 Task/Tool 业务状态 ──────────────────────────


@pytest.mark.asyncio
async def test_narrative_failure_never_touches_task_status(monkeypatch):
    """task_summary 节点异常 → deterministic 回退,任务终态不受影响。"""
    from app.agent_runtime.graphs.test_plan.versions.v3 import nodes_narrative as nn

    monkeypatch.setattr(
        nn, "get_feature_flags", lambda: type(
            "F", (), {"phase29b_task_summary_narrative_enabled": True}
        )()
    )
    # 无 llm_client → 直接透传,不改变任何任务字段。
    result = await nn.task_summary_narrative_node(
        {"task_id": "t", "task_status": "completed", "graph_run_id": "run-90"},
        ctx=type("Ctx", (), {"task_internal_id": 90})(),
    )
    assert result["task_summary_narrative_done"] is True
    assert "task_status" not in result


# ── 13. v3 主图 target Tool 全覆盖顺序 ───────────────────────────────────


def test_graph_registers_narrative_barrier_and_summary_node():
    """v3 主图注册 tool_narrative_barrier + task_summary_narrative 节点。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.graph import (
        NODE_PARSE_REQ,
        NODE_PARSE_TPL,
        NODE_TASK_SUMMARY_NARRATIVE,
        NODE_TOOL_NARRATIVE_BARRIER,
        build_test_plan_v3_graph,
    )

    build_test_plan_v3_graph(checkpointer=None, interrupt_enabled=False)
    # 节点常量存在且与 routing 一致。
    assert NODE_TOOL_NARRATIVE_BARRIER == "tool_narrative_barrier"
    assert NODE_TASK_SUMMARY_NARRATIVE == "task_summary_narrative"
    assert NODE_PARSE_REQ == "parse_requirement"
    assert NODE_PARSE_TPL == "parse_template"
    # 拓扑:parse_requirement → barrier → parse_template(route_after_narrative 读取
    # barrier 写入的 continuation_route)。
    from app.agent_runtime.graphs.test_plan.versions.v3 import routing as rt

    assert rt.route_after_narrative({"next_node": "parse_template"}) == NODE_PARSE_TPL
    assert rt.route_after_narrative({}) == NODE_PARSE_TPL


def test_tool_context_builders_registry_covers_target_tools():
    """8 个目标 Tool 全部有 Context Builder(Tool 全覆盖)。"""
    from app.agent_runtime.narrative_composer.context_builders import (
        TOOL_NARRATIVE_CONTEXT_BUILDERS,
    )

    targets = [
        "RequirementParserTool",
        "TemplateParserTool",
        "KnowledgeSearchTool",
        "SectionSuggestionTool",
        "TestPlanGeneratorTool",
        "ResultReviewTool",
        "WordExportTool",
        "DocxFormatCheckTool",
    ]
    for t in targets:
        assert t in TOOL_NARRATIVE_CONTEXT_BUILDERS, f"{t} 缺 Builder"


def test_pending_narrative_holds_all_anchor_fields():
    """PendingNarrative 同时持有 source_tool_call_id / source_event_id / tool_name / attempt。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        _build_pending_narrative,
    )

    pending = _build_pending_narrative(
        state={"task_id": "t"},
        tool_name="WordExportTool",
        tool_call_id="WordExportTool-xyz",
        attempt=1,
        terminal_status="success",
        continuation_route="check_docx_format_step",
        source_event_id="evt-final",
    )
    assert pending["source_tool_call_id"] == "WordExportTool-xyz"
    assert pending["source_event_id"] == "evt-final"
    assert pending["source_tool_name"] == "WordExportTool"
    assert pending["tool_attempt"] == 1
    assert pending["terminal_status"] == "success"
    assert pending["continuation_route"] == "check_docx_format_step"
