"""Test: 11. 事件载荷脱敏 — 不应泄漏 raw LLM 文本、args_signature 等敏感字段。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent_runtime.incremental.event_emitter import IncrementalEventEmitter
from app.agent_runtime.incremental.schemas import BudgetState, IncrementalResult, PublicSummary


class _Sink:
    def __init__(self):
        self.events = []

    async def emit(self, **kw):
        self.events.append(kw)
        return kw


@pytest.mark.asyncio
async def test_incremental_event_emitter_started_sanitized():
    sink = _Sink()
    emitter = IncrementalEventEmitter(ctx=_Ctx(sink), node_name="incremental_subgraph")
    intent = _StubIntent()
    await emitter.emit_started(
        task_id="t1",
        graph_run_id="r1",
        intent=intent,
    )
    ev = sink.events[-1]
    # 1. 不含 raw LLM text
    assert "raw_text" not in ev["payload"]
    assert "llm_response" not in ev["payload"]
    # 2. payload key 限制在 allowlist(顶层)
    allowed = {"task_id", "graph_run_id", "node_name", "event_type", "title", "content", "payload"}
    extra_keys = set(ev.keys()) - allowed
    assert extra_keys == set()


@pytest.mark.asyncio
async def test_incremental_event_emitter_started_includes_visible_execution_plan():
    sink = _Sink()
    emitter = IncrementalEventEmitter(ctx=_Ctx(sink), node_name="incremental_subgraph")

    await emitter.emit_started(
        task_id="t1",
        graph_run_id="r1",
        intent=_StubIntent(),
    )

    assert [event["event_type"] for event in sink.events] == [
        "incremental_started",
        "plan_created",
    ]
    plan = sink.events[-1]["payload"]["plan"]["steps"]
    assert [step["step_id"] for step in plan] == ["generate", "review", "export"]
    assert "s1" in plan[0]["detail"]


@pytest.mark.asyncio
async def test_incremental_event_emitter_completed_sanitized():
    sink = _Sink()
    emitter = IncrementalEventEmitter(ctx=_Ctx(sink), node_name="incremental_subgraph")
    await emitter.emit_completed(
        task_id="t1",
        graph_run_id="r1",
        result=_StubResult(),
    )
    ev = sink.events[-1]
    payload = ev["payload"]
    # 不应泄漏 internal_id / session / client / engine
    for forbidden in ("session", "client", "internal_id", "engine"):
        assert forbidden not in payload


@pytest.mark.asyncio
async def test_incremental_event_emitter_completed_accepts_terminal_metadata():
    sink = _Sink()
    emitter = IncrementalEventEmitter(ctx=_Ctx(sink), node_name="incremental_subgraph")
    await emitter.emit_completed(
        task_id="t1",
        graph_run_id="r1",
        result=_StubResult(),
        extra={
            "summary": "基于既有测试方案完成增量修改。",
            "summary_facts": {"generated_sections": 2},
            "artifact": {"public_id": "artifact-v2"},
            "format_check": {"status": "passed"},
            "started_at": "2026-09-03T12:00:00Z",
            "completed_at": "2026-09-03T12:00:18Z",
            "duration_ms": 18000,
        },
    )

    payload = sink.events[-1]["payload"]
    assert payload["summary_facts"]["generated_sections"] == 2
    assert payload["artifact"]["public_id"] == "artifact-v2"
    assert payload["duration_ms"] == 18000


@pytest.mark.asyncio
async def test_incremental_subgraph_runs_export_and_format_check_before_terminal(monkeypatch):
    from app.agent_runtime.incremental import agent_loop as agent_loop_module
    from app.agent_runtime.incremental.subgraph import run_incremental_subgraph

    class _Adapter:
        def __init__(self):
            self.calls = []

        async def execute(self, *, tool_name, **_kwargs):
            self.calls.append(tool_name)
            if tool_name == "WordExportTool":
                return {
                    "success": True,
                    "data": {
                        "artifact": {
                            "public_id": "artifact-v2",
                            "artifact_type": "test_plan_word",
                            "file_name": "plan.docx",
                            "storage_path": "artifacts/1/1/plan.docx",
                        }
                    },
                }
            return {"success": True, "data": {"level": "passed", "losses": []}}

    async def fake_run(_state, *, ctx):
        return IncrementalResult(
            success=True,
            new_artifact_public_id=None,
            new_artifact_version_no=None,
            superseded_artifact_public_ids=[],
            modified_section_ids=["s1"],
            tool_calls_used=2,
            rounds_used=1,
            public_summary=PublicSummary(headline="done", detail="done"),
            budget_state=BudgetState(
                steps=1, tool_calls=2, wall_seconds=0, token_estimate=0, repeated_tool_calls=0,
            ),
        )

    sink = _Sink()
    ctx = _Ctx(sink)
    ctx.tool_adapter = _Adapter()
    ctx.task_internal_id = 7
    monkeypatch.setattr(agent_loop_module, "run_incremental", fake_run)
    async def fake_summary(**_kwargs):
        return None

    monkeypatch.setattr(
        "app.agent_runtime.incremental.subgraph._generate_incremental_task_summary",
        fake_summary,
    )

    result = await run_incremental_subgraph(
        {"task_id": "t1", "graph_run_id": "r1", "test_plan_content": {"generated_sections": []}},
        ctx=ctx,
        config={"configurable": {"thread_id": "t1"}},
    )

    assert ctx.tool_adapter.calls == ["WordExportTool", "DocxFormatCheckTool"]
    assert result["task_status"] == "completed"
    terminal = next(event for event in sink.events if event["event_type"] == "task_completed")
    assert terminal["payload"]["artifact"]["public_id"] == "artifact-v2"
    assert terminal["payload"]["duration_ms"] >= 0
    assert [event["event_type"] for event in sink.events].index("task_completed") < [
        event["event_type"] for event in sink.events
    ].index("incremental_completed")


@pytest.mark.asyncio
async def test_incremental_subgraph_normalizes_word_export_artifact_id(monkeypatch):
    """The real WordExportTool returns a flat data artifact, not data.artifact."""
    from app.agent_runtime.incremental import agent_loop as agent_loop_module
    from app.agent_runtime.incremental.subgraph import run_incremental_subgraph

    class _Adapter:
        def __init__(self):
            self.calls = []

        async def execute(self, *, tool_name, **_kwargs):
            self.calls.append(tool_name)
            if tool_name == "WordExportTool":
                return {
                    "success": True,
                    "data": {
                        "artifact_id": "art-real-shape",
                        "artifact_type": "test_plan_word",
                        "file_name": "plan.docx",
                        "file_ext": ".docx",
                        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "file_size": 1234,
                        "storage_path": "artifacts/1/1/plan.docx",
                        "download_url": "/api/v1/artifacts/art-real-shape/download",
                        "version_no": 2,
                    },
                }
            return {"success": True, "data": {"level": "passed", "losses": []}}

    async def fake_run(_state, *, ctx):
        return IncrementalResult(
            success=True,
            new_artifact_public_id=None,
            new_artifact_version_no=None,
            superseded_artifact_public_ids=[],
            modified_section_ids=["s1"],
            tool_calls_used=2,
            rounds_used=1,
            public_summary=PublicSummary(headline="done", detail="done"),
            budget_state=BudgetState(
                steps=1, tool_calls=2, wall_seconds=0, token_estimate=0, repeated_tool_calls=0,
            ),
        )

    sink = _Sink()
    ctx = _Ctx(sink)
    ctx.tool_adapter = _Adapter()
    ctx.task_internal_id = 8
    monkeypatch.setattr(agent_loop_module, "run_incremental", fake_run)

    async def fake_summary(**_kwargs):
        return None

    monkeypatch.setattr(
        "app.agent_runtime.incremental.subgraph._generate_incremental_task_summary",
        fake_summary,
    )

    result = await run_incremental_subgraph(
        {"task_id": "t2", "graph_run_id": "r2", "test_plan_content": {"generated_sections": []}},
        ctx=ctx,
        config={"configurable": {"thread_id": "t2"}},
    )

    assert ctx.tool_adapter.calls == ["WordExportTool", "DocxFormatCheckTool"]
    assert result["task_status"] == "completed"
    terminal = next(event for event in sink.events if event["event_type"] == "task_completed")
    assert terminal["payload"]["artifact"]["public_id"] == "art-real-shape"


class _StubIntent:
    """minimal intent stub for emit_started"""

    class _ScopeStub:
        kind = "modify_section"
        target_section_ids = ["s1"]

    class _ArtifactStub:
        artifact_public_id = "artifact-abc123"
        version_no = 1

    scope = _ScopeStub()
    existing_artifact = _ArtifactStub()
    confidence = 0.9
    raw_user_message = "stub"


class _StubResult:
    """minimal IncrementalResult-like stub for emit_completed"""
    success = True
    new_artifact_public_id = "artifact-abc123"
    new_artifact_version_no = 2
    superseded_artifact_public_ids = []
    modified_section_ids = ["s1"]
    tool_calls_used = 3
    rounds_used = 1
    public_summary = PublicSummary(headline="h", detail="d")


class _Ctx:
    def __init__(self, sink):
        self.event_sink = sink
        self.session_factory = lambda: _NullCM()


@pytest.mark.asyncio
async def test_incremental_task_summary_uses_tag_preserving_profile(monkeypatch):
    """Incremental summaries must not fall back to the bridge's 24-char title parser."""
    import app.agent_runtime.feature_flags as feature_flags_module
    from app.agent_runtime.graphs.test_plan.versions.v3 import nodes_narrative
    from app.agent_runtime.incremental import subgraph
    from app.agent_runtime.narrative_composer import composer as composer_module
    from app.agent_runtime.narrative_composer.schemas import (
        NarrativeFactConstraints,
        TaskSummaryNarrativeContext,
    )
    from app.llm.task_profiles import (
        LLMParserType,
        TASK_SUMMARY_NARRATIVE_COMPOSER_PROFILE,
    )

    summary_context = TaskSummaryNarrativeContext(
        task_id="task-summary-profile",
        graph_run_id="run-summary-profile",
        task_status="completed",
        task_goal="revise an existing plan",
        fact_constraints=NarrativeFactConstraints(),
    )
    captured = {}

    class _Composer:
        def __init__(self, *_args, **_kwargs):
            pass

        async def _run_generation(self, **kwargs):
            captured.update(kwargs)
            return None

    monkeypatch.setattr(
        feature_flags_module,
        "get_feature_flags",
        lambda: SimpleNamespace(
            phase29b_task_summary_narrative_enabled=True,
            phase29b_narrative_timeout_seconds=10,
            phase29b_narrative_repair_attempts=1,
        ),
    )
    monkeypatch.setattr(
        nodes_narrative, "_resolve_narrative_llm", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        nodes_narrative,
        "_build_task_summary_context",
        lambda _state: summary_context,
    )
    monkeypatch.setattr(composer_module, "NarrativeComposer", _Composer)

    await subgraph._generate_incremental_task_summary(
        state_dict={"task_id": "task-summary-profile", "graph_run_id": "run-summary-profile"},
        ctx=SimpleNamespace(event_sink=_Sink(), task_internal_id=1),
    )

    assert captured["profile"] is TASK_SUMMARY_NARRATIVE_COMPOSER_PROFILE
    assert captured["profile"].parser is LLMParserType.MARKDOWN


class _NullCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False
