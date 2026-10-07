"""Test: 10. Fallback 路径 — 当 LLM 多次违规或 budget 耗尽,回退到 legacy regen 字节级路径。"""

from __future__ import annotations

import pytest

from app.agent_runtime.incremental.agent_loop import run_incremental
from app.agent_runtime.incremental.fallback import (
    _build_incremental_regen_state,
    _invoke_regen_node,
    run_legacy_incremental_fallback,
)

from .conftest import build_intent, build_state


class _StubCtx:
    def __init__(self, llm_client, tool_adapter, session_factory=None):
        self.llm_client = llm_client
        self.tool_adapter = tool_adapter
        self.event_sink = _NullSink()
        self.session_factory = session_factory or (lambda: _NullCM())
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
async def test_fallback_legacy_regen_runs_standalone(make_fake_llm, make_stub_adapter):
    """fallback.run_legacy_incremental_fallback 必须独立可调,不依赖 agent_loop。

    返回 dict(state delta)结构。失败时 ``incremental_result.success=False`` +
    ``incremental_result.fallback_reason`` 仍填充 —— 不抛。
    """
    intent = build_intent(target_section_ids=["s1"], kind="modify_section")
    state = build_state(intent=intent)

    llm = make_fake_llm([])  # 不会被调
    adapter = make_stub_adapter({})
    ctx = _StubCtx(llm_client=llm, tool_adapter=adapter)

    result = await run_legacy_incremental_fallback(
        state, ctx=ctx, failure_reason="standalone_test",
    )

    assert isinstance(result, dict)
    assert "incremental_result" in result
    inner = result["incremental_result"]
    assert "success" in inner
    # 不论 success 真值,fallback 必须返回结构化对象 + 填充 fallback_reason
    assert inner["fallback_reason"] is not None
    assert isinstance(inner["fallback_reason"], str)


@pytest.mark.asyncio
async def test_agent_loop_returns_decision_failure_when_banned_tool_repeated(
    make_fake_llm, make_stub_adapter,
):
    """决策性失败(banned / scope / permission / schema)不再触发 legacy fallback,
    直接返回 success=False + fallback_reason=None。fallback 仅在「真执行失败」
    (budget exceeded / loop crash)时才触发。

    原因:banned tool 调用被 filter 拦截后,legacy regen 仍会跑一遍
    TestPlanRegenTool,既浪费又给用户造成「多余的 TestPlanRegenTool 又跑了一次」
    错觉。
    """
    intent = build_intent(target_section_ids=["s1"], kind="modify_section")
    state = build_state(intent=intent)

    # LLM 始终调 banned tool (RequirementParserTool) → filter 直接 fail → 不走 fallback
    responses = [
        '{"action":"call_tool","tool_name":"RequirementParserTool",'
        '"tool_arguments":{},"target_section_ids":[],'
        '"scope_kind":"modify_section","decision_summary":"s","public_update":"u","confidence":0.9}'
        for _ in range(10)
    ]
    llm = make_fake_llm(responses)
    adapter = make_stub_adapter({})
    ctx = _StubCtx(llm_client=llm, tool_adapter=adapter)

    result = await run_incremental(state, ctx=ctx)
    assert result.success is False
    # 决策性失败 → 不走 fallback,fallback_reason 必须是 None
    assert result.fallback_reason is None
    # 必须有明确的失败摘要
    assert result.public_summary is not None
    # headline 或 detail 中至少有一个包含「拦截 / 决策 / 失败」之类的提示
    haystack = (result.public_summary.headline or "") + (result.public_summary.detail or "")
    assert any(kw in haystack for kw in ("拦截", "决策", "无法继续", "失败"))


def test_incremental_fallback_synthesizes_review_issues_from_intent():
    intent = build_intent(
        target_section_ids=["body_18_level_1"],
        kind="modify_section",
        raw_user_message="项目概述章节的内容太少了，需要更加丰富，起码达到100字才可以",
    )
    state = build_state(intent=intent)

    fallback_state = _build_incremental_regen_state(
        state,
        failure_reason="llm_parse_error",
    )

    issues = fallback_state["review_result"]["review_issues"]
    assert issues
    assert issues[0]["section_id"] == "body_18_level_1"
    assert issues[0]["rule_id"] == "incremental_user_request"
    assert "项目概述章节" in issues[0]["message"]


@pytest.mark.asyncio
async def test_incremental_fallback_seeds_runtime_context_for_legacy_regen(
    monkeypatch, make_fake_llm, make_stub_adapter,
):
    """Direct legacy regen fallback must not call v2 node with empty ctx state."""
    from app.agent_runtime.graphs.test_plan.versions.v2 import nodes_review_format

    intent = build_intent(target_section_ids=["s1"], kind="modify_section")
    state = build_state(intent=intent)
    state["test_plan_content"] = {
        "section_package": {"generated_sections": [{"section_id": "s1"}]},
    }
    state["template_structure"] = {"generation_config": {"ai_fields": []}}
    state["review_result"] = {
        "level": "failed",
        "review_issues": [{"section_id": "s1", "message": "too short"}],
    }

    async def fake_regenerate_sections_node(state_dict, *, ctx):
        seeded = getattr(ctx, "_intermediate_state", {})
        assert seeded["test_plan_content"] == state["test_plan_content"]
        assert seeded["template_structure"] == state["template_structure"]
        assert seeded["review_result"] == state["review_result"]
        return {
            "artifact": {"storage_path": "new.docx"},
            "test_plan_content": {"modified_section_ids": ["s1"]},
        }

    monkeypatch.setattr(
        nodes_review_format,
        "regenerate_sections_node",
        fake_regenerate_sections_node,
    )

    ctx = _StubCtx(
        llm_client=make_fake_llm([]),
        tool_adapter=make_stub_adapter({}),
    )
    ctx._intermediate_state = {"preexisting": "keep"}

    result = await _invoke_regen_node(state, ctx=ctx)

    assert result["success"] is True
    assert result["storage_path"] == "new.docx"
    assert result["modified_section_ids"] == ["s1"]
    assert ctx._intermediate_state == {"preexisting": "keep"}
