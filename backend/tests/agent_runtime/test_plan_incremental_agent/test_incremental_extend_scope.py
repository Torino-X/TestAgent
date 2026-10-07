"""Test: 3. extend_scope kind path."""

from __future__ import annotations

import pytest

from app.agent_runtime.incremental.agent_loop import run_incremental

from .conftest import (
    build_intent,
    build_state,
    format_check_envelope_ok,
    regen_envelope_ok,
    review_envelope_passed,
    word_export_envelope_ok,
)


class _StubCtx:
    def __init__(self, llm_client, tool_adapter):
        self.llm_client = llm_client
        self.tool_adapter = tool_adapter
        self.event_sink = _NullSink()
        self.session_factory = lambda: _NullCM()
        self.cancellation_service = _NoopCancel()


class _NullSink:
    events = []

    async def emit(self, **kw):
        self.events.append(kw)
        return kw


class _NullCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _NoopCancel:
    def is_cancelled(self, task_id):
        return False


@pytest.mark.asyncio
async def test_extend_scope_kind(make_fake_llm, make_stub_adapter):
    intent = build_intent(
        target_section_ids=["s1"],
        kind="extend_scope",
        allow_extra_sections=True,
    )
    state = build_state(intent=intent)

    # LLM 在 extend_scope + allow_extra=True 时,可以新增 s4
    llm = make_fake_llm([
        '{"action":"call_tool","tool_name":"TestPlanRegenTool",'
        '"tool_arguments":{"section_ids":["s1","s4"],"issues":[]},'
        '"target_section_ids":["s1","s4"],"scope_kind":"extend_scope",'
        '"decision_summary":"扩展章节","public_update":"扩展中","confidence":0.9}',
        '{"action":"call_tool","tool_name":"ResultReviewTool",'
        '"tool_arguments":{},"target_section_ids":[],'
        '"scope_kind":"extend_scope","decision_summary":"复审",'
        '"public_update":"复审中","confidence":0.9}',
        '{"action":"call_tool","tool_name":"WordExportTool",'
        '"tool_arguments":{"artifact_public_id":"artifact-abc123",'
        '"version_no":2,"template_file_id":"tpl-1"},'
        '"target_section_ids":[],"scope_kind":"extend_scope",'
        '"decision_summary":"导出","public_update":"导出中","confidence":0.9}',
        '{"action":"call_tool","tool_name":"DocxFormatCheckTool",'
        '"tool_arguments":{"artifact_public_id":"artifact-abc123"},'
        '"target_section_ids":[],"scope_kind":"extend_scope",'
        '"decision_summary":"格式检查","public_update":"检查中","confidence":0.9}',
        '{"action":"finish","decision_summary":"完成","public_update":"完成","confidence":0.9}',
    ])
    adapter = make_stub_adapter({
        "TestPlanRegenTool": [regen_envelope_ok(["s1", "s4"])],
        "ResultReviewTool": [review_envelope_passed()],
        "WordExportTool": [word_export_envelope_ok("artifact-abc123", 2)],
        "DocxFormatCheckTool": [format_check_envelope_ok()],
    })
    ctx = _StubCtx(llm_client=llm, tool_adapter=adapter)
    result = await run_incremental(state, ctx=ctx)

    assert result.success is True
    assert "s4" in result.modified_section_ids
    # scope_kind=extend_scope 必须回传,scope_guard 校验通过
    assert result.fallback_reason is None