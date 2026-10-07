"""Test: 2. Multi-section modify path(≤ MAX_INCREMENTAL_TARGET_SECTIONS=8)."""

from __future__ import annotations

import pytest

from app.agent_runtime.incremental.agent_loop import run_incremental
from app.agent_runtime.incremental.scope_guard import MAX_INCREMENTAL_TARGET_SECTIONS

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
async def test_multi_section_modify(make_fake_llm, make_stub_adapter):
    targets = ["s1", "s2", "s3"]
    intent = build_intent(target_section_ids=targets, kind="modify_section")
    state = build_state(intent=intent)

    llm = make_fake_llm([
        '{"action":"call_tool","tool_name":"TestPlanRegenTool",'
        '"tool_arguments":{"section_ids":["s1","s2","s3"],"issues":[]},'
        '"target_section_ids":["s1","s2","s3"],'
        '"scope_kind":"modify_section","decision_summary":"重写多 section",'
        '"public_update":"正在重写","confidence":0.9}',
        '{"action":"call_tool","tool_name":"ResultReviewTool",'
        '"tool_arguments":{},"target_section_ids":[],'
        '"scope_kind":"modify_section","decision_summary":"复审",'
        '"public_update":"正在复审","confidence":0.9}',
        '{"action":"call_tool","tool_name":"WordExportTool",'
        '"tool_arguments":{"artifact_public_id":"artifact-abc123",'
        '"version_no":2,"template_file_id":"tpl-1"},'
        '"target_section_ids":[],"scope_kind":"modify_section",'
        '"decision_summary":"导出","public_update":"导出中","confidence":0.9}',
        '{"action":"call_tool","tool_name":"DocxFormatCheckTool",'
        '"tool_arguments":{"artifact_public_id":"artifact-abc123"},'
        '"target_section_ids":[],"scope_kind":"modify_section",'
        '"decision_summary":"格式检查","public_update":"检查中","confidence":0.9}',
        '{"action":"finish","decision_summary":"完成","public_update":"完成","confidence":0.9}',
    ])

    adapter = make_stub_adapter({
        "TestPlanRegenTool": [regen_envelope_ok(targets)],
        "ResultReviewTool": [review_envelope_passed()],
        "WordExportTool": [word_export_envelope_ok("artifact-abc123", 2)],
        "DocxFormatCheckTool": [format_check_envelope_ok()],
    })
    ctx = _StubCtx(llm_client=llm, tool_adapter=adapter)

    result = await run_incremental(state, ctx=ctx)

    assert result.success is True
    assert set(result.modified_section_ids) >= set(targets)
    assert len(targets) <= MAX_INCREMENTAL_TARGET_SECTIONS