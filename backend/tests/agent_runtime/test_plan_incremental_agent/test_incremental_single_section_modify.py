"""Test: 1. Single section issue happy path."""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from app.agent_runtime.incremental.agent_loop import run_incremental
from app.agent_runtime.incremental.schemas import (
    BudgetState,
    IncrementalResult,
    PublicSummary,
)


def _profile_result(parsed: Dict[str, Any]):
    from app.integrations.llm_client import LLMProfileResult

    raw = json.dumps(parsed, ensure_ascii=False)
    return LLMProfileResult(
        task_name="incremental_agent",
        raw_text=raw,
        parsed=parsed,
        success=True,
        error_type=None,
        error_message=None,
    )

from .conftest import (
    StubToolAdapter,
    build_intent,
    build_state,
    format_check_envelope_ok,
    regen_envelope_ok,
    review_envelope_passed,
    word_export_envelope_ok,
)


class _StubCtx:
    """最小 RuntimeContext 替身;只满足 agent_loop 字段访问。"""

    def __init__(
        self,
        llm_client,
        tool_adapter: StubToolAdapter,
        event_sink=None,
        session_factory=None,
    ):
        self.llm_client = llm_client
        self.tool_adapter = tool_adapter
        self.event_sink = event_sink or _NullSink()
        self.session_factory = session_factory or (lambda: _NullCM())
        self.cancellation_service = _NoopCancel()


class _NullSink:
    events: list = []

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
async def test_single_section_modify_happy_path(make_fake_llm, make_stub_adapter):
    intent = build_intent(target_section_ids=["s1"], kind="modify_section")
    state = build_state(intent=intent)

    # R8 — incremental_agent 跑两步即强制 finish。
    # WordExportTool / DocxFormatCheckTool 不再由 incremental_agent 决策调用,
    # 改由 subgraph.py:_export_and_check_after_incremental(R6)接管。
    llm = make_fake_llm([
        '{"action":"call_tool","tool_name":"TestPlanRegenTool",'
        '"tool_arguments":{"section_ids":["s1"],"issues":[]},'
        '"target_section_ids":["s1"],"scope_kind":"modify_section",'
        '"decision_summary":"重写 s1",'
        '"public_update":"正在重写 s1",'
        '"confidence":0.9}',
        '{"action":"call_tool","tool_name":"ResultReviewTool",'
        '"tool_arguments":{},"target_section_ids":["s1"],'
        '"scope_kind":"modify_section","decision_summary":"局部复审",'
        '"public_update":"正在复审","confidence":0.9}',
    ])

    adapter = make_stub_adapter({
        "TestPlanRegenTool": [regen_envelope_ok(["s1"])],
        "ResultReviewTool": [review_envelope_passed()],
    })

    ctx = _StubCtx(llm_client=llm, tool_adapter=adapter)
    result = await run_incremental(state, ctx=ctx)

    assert result.success is True
    # R6 接管导出,agent_loop 阶段 new_artifact_public_id 还没值(R6 后续写)
    assert result.new_artifact_public_id is None
    assert result.new_artifact_version_no is None
    assert "s1" in result.modified_section_ids
    assert isinstance(result.public_summary, PublicSummary)
    assert result.fallback_reason is None
    # R8: 只能确认 TestPlanRegenTool + ResultReviewTool 被调
    assert [c["tool_name"] for c in adapter.calls] == [
        "TestPlanRegenTool",
        "ResultReviewTool",
    ]
    assert adapter.calls[0]["tool_name"] == "TestPlanRegenTool"
    assert adapter.calls[0]["inputs"]["issues"][0]["rule_id"] == (
        "incremental_user_request"
    )
    assert adapter.calls[0]["inputs"]["issues"][0]["section_id"] == "s1"


@pytest.mark.asyncio
async def test_incremental_accepts_json_restored_intent(make_fake_llm, make_stub_adapter):
    """Outbox/LangGraph 恢复出的 incremental_intent 是 dict 时也应正常执行。"""
    intent = build_intent(target_section_ids=["s1"], kind="modify_section")
    state = build_state(intent=intent)
    state["incremental_intent"] = intent.model_dump(mode="json")

    llm = make_fake_llm([
        '{"action":"call_tool","tool_name":"TestPlanRegenTool",'
        '"tool_arguments":{"section_ids":["s1"],"issues":[]},'
        '"target_section_ids":["s1"],"scope_kind":"modify_section",'
        '"decision_summary":"重写 s1",'
        '"public_update":"正在重写 s1",'
        '"confidence":0.9}',
        '{"action":"call_tool","tool_name":"ResultReviewTool",'
        '"tool_arguments":{},"target_section_ids":["s1"],'
        '"scope_kind":"modify_section","decision_summary":"局部复审",'
        '"public_update":"正在复审","confidence":0.9}',
    ])

    adapter = make_stub_adapter({
        "TestPlanRegenTool": [regen_envelope_ok(["s1"])],
        "ResultReviewTool": [review_envelope_passed()],
    })

    ctx = _StubCtx(llm_client=llm, tool_adapter=adapter)
    result = await run_incremental(state, ctx=ctx)

    assert result.success is True
    assert adapter.calls[0]["tool_name"] == "TestPlanRegenTool"


@pytest.mark.asyncio
async def test_incremental_accepts_profile_result_from_real_llm_client(
    make_fake_llm, make_stub_adapter,
):
    """Production LLMClient returns LLMProfileResult, not a raw JSON string."""
    intent = build_intent(target_section_ids=["s1"], kind="modify_section")
    state = build_state(intent=intent)

    llm = make_fake_llm([
        _profile_result({
            "action": "call_tool",
            "tool_name": "TestPlanRegenTool",
            "tool_arguments": {"section_ids": ["s1"], "issues": []},
            "target_section_ids": ["s1"],
            "scope_kind": "modify_section",
            "decision_summary": "重写 s1",
            "public_update": "正在重写 s1",
            "confidence": 0.9,
        }),
        _profile_result({
            "action": "call_tool",
            "tool_name": "ResultReviewTool",
            "tool_arguments": {},
            "target_section_ids": ["s1"],
            "scope_kind": "modify_section",
            "decision_summary": "局部复审",
            "public_update": "正在复审",
            "confidence": 0.9,
        }),
    ])

    adapter = make_stub_adapter({
        "TestPlanRegenTool": [regen_envelope_ok(["s1"])],
        "ResultReviewTool": [review_envelope_passed()],
    })

    ctx = _StubCtx(llm_client=llm, tool_adapter=adapter)
    result = await run_incremental(state, ctx=ctx)

    assert result.success is True
    assert result.fallback_reason is None
    # R8: incremental_agent 跑到 review success 即强制 finish,
    # WordExportTool / DocxFormatCheckTool 由 subgraph R6 接管。
    assert [c["tool_name"] for c in adapter.calls] == [
        "TestPlanRegenTool",
        "ResultReviewTool",
    ]


@pytest.mark.asyncio
async def test_incremental_normalizes_regen_alias_args_and_passes_graph_state(
    make_fake_llm, make_stub_adapter,
):
    """LLM alias/extra args must not block valid incremental regen."""
    intent = build_intent(target_section_ids=["s1"], kind="modify_section")
    state = build_state(intent=intent)

    llm = make_fake_llm([
        _profile_result({
            "action": "call_tool",
            "tool_name": "TestPlanRegenTool",
            "tool_arguments": {
                "target_section_ids": ["s1"],
                "issues": [{"section_id": "s1", "message": "too short"}],
                "test_plan_content": {"should": "be dropped"},
                "template_structure": {"should": "be dropped"},
            },
            "target_section_ids": ["s1"],
            "scope_kind": "modify_section",
            "decision_summary": "重写 s1",
            "public_update": "正在重写 s1",
            "confidence": 0.9,
        }),
        _profile_result({
            "action": "call_tool",
            "tool_name": "ResultReviewTool",
            "tool_arguments": {"section_ids": ["s1"]},
            "target_section_ids": ["s1"],
            "scope_kind": "modify_section",
            "decision_summary": "局部复审",
            "public_update": "正在复审",
            "confidence": 0.9,
        }),
        _profile_result({
            "action": "call_tool",
            "tool_name": "WordExportTool",
            "tool_arguments": {
                "artifact_public_id": "artifact-abc123",
                "version_no": 2,
                "template_file_id": "tpl-1",
            },
            "target_section_ids": [],
            "scope_kind": "modify_section",
            "decision_summary": "导出新版本",
            "public_update": "正在导出",
            "confidence": 0.9,
        }),
        _profile_result({
            "action": "call_tool",
            "tool_name": "DocxFormatCheckTool",
            "tool_arguments": {
                "artifact_public_id": "artifact-abc123",
                "section_ids": ["s1"],
            },
            "target_section_ids": ["s1"],
            "scope_kind": "modify_section",
            "decision_summary": "格式检查",
            "public_update": "检查中",
            "confidence": 0.9,
        }),
        _profile_result({
            "action": "finish",
            "decision_summary": "完成",
            "public_update": "增量任务完成",
            "confidence": 0.9,
        }),
    ])

    adapter = make_stub_adapter({
        "TestPlanRegenTool": [regen_envelope_ok(["s1"])],
        "ResultReviewTool": [review_envelope_passed()],
        "WordExportTool": [word_export_envelope_ok("artifact-abc123", 2)],
        "DocxFormatCheckTool": [format_check_envelope_ok()],
    })

    ctx = _StubCtx(llm_client=llm, tool_adapter=adapter)
    result = await run_incremental(state, ctx=ctx)

    assert result.success is True
    regen_call = adapter.calls[0]
    assert regen_call["tool_name"] == "TestPlanRegenTool"
    assert regen_call["graph_state_present"] is True
    assert regen_call["inputs"] == {
        "section_ids": ["s1"],
        "issues": [{"section_id": "s1", "message": "too short"}],
        "generation_config_subset": {},
    }
    review_call = adapter.calls[1]
    assert review_call["inputs"] == {
        "target_section_ids": ["s1"],
        "review_standard": {},
    }


@pytest.mark.asyncio
async def test_incremental_subgraph_runner_preserves_runtime_context(
    monkeypatch, make_stub_adapter,
):
    """生产 runner 必须显式把 RuntimeContext 传进 agent_loop。

    LangGraph 不会把自定义 ``ctx`` kwarg 自动注入节点。这个测试防止
    runner 回退到 compiled.ainvoke 后再次出现 ctx=None。
    """
    from app.agent_runtime.incremental import agent_loop as agent_loop_mod
    from app.agent_runtime.incremental.subgraph import (
        NODE_INCREMENTAL_FINISH,
        run_incremental_subgraph,
    )

    intent = build_intent(target_section_ids=["s1"], kind="modify_section")
    state = build_state(intent=intent)
    adapter = make_stub_adapter()
    runtime_ctx = _StubCtx(llm_client=object(), tool_adapter=adapter)
    captured: Dict[str, Any] = {}

    async def fake_run_incremental(state_dict, *, ctx, **_kwargs):
        captured["ctx"] = ctx
        state_dict.setdefault("incremental_steps", []).append({
            "step": "fake",
            "ctx_present": ctx is not None,
        })
        return IncrementalResult(
            success=True,
            new_artifact_public_id="artifact-new",
            new_artifact_version_no=2,
            superseded_artifact_public_ids=[],
            modified_section_ids=["s1"],
            tool_calls_used=1,
            rounds_used=1,
            public_summary=PublicSummary(headline="增量任务已完成"),
            fallback_reason=None,
            budget_state=BudgetState(
                steps=1,
                tool_calls=1,
                wall_seconds=0.0,
                token_estimate=0,
                repeated_tool_calls=0,
            ),
        )

    monkeypatch.setattr(agent_loop_mod, "run_incremental", fake_run_incremental)

    result_state = await run_incremental_subgraph(
        state,
        ctx=runtime_ctx,
        config={"configurable": {"thread_id": "test-task"}},
    )

    assert captured["ctx"] is runtime_ctx
    assert result_state["task_status"] == "completed"
    assert result_state["current_node"] == NODE_INCREMENTAL_FINISH
    assert result_state["incremental_result"]["success"] is True
    assert result_state["incremental_steps"][0]["ctx_present"] is True


@pytest.mark.asyncio
async def test_incremental_subgraph_runner_fails_fast_without_runtime_context():
    from app.agent_runtime.incremental.subgraph import run_incremental_subgraph

    intent = build_intent(target_section_ids=["s1"], kind="modify_section")
    state = build_state(intent=intent)

    with pytest.raises(RuntimeError, match="incremental_runtime_context_missing"):
        await run_incremental_subgraph(
            state,
            ctx=None,
            config={"configurable": {"thread_id": "test-task"}},
        )
