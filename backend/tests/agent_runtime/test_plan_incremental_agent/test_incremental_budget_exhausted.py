"""Test: 9. budget exhaust 路径 — 8 步/6 工具/180s 内仍未满足 success_requires."""

from __future__ import annotations

import pytest

from app.agent_runtime.incremental.agent_loop import run_incremental

from .conftest import build_intent, build_state


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
async def test_budget_steps_exhausted(make_fake_llm, make_stub_adapter):
    intent = build_intent(target_section_ids=["s1"], kind="modify_section")
    state = build_state(intent=intent)

    # 持续返回 call_tool,从未 finish;MAX_AGENT_STEPS=8 时 BudgetExceeded
    responses = [
        '{"action":"call_tool","tool_name":"TestPlanRegenTool",'
        '"tool_arguments":{"section_ids":["s1"],"issues":[]},'
        '"target_section_ids":["s1"],"scope_kind":"modify_section",'
        '"decision_summary":"s","public_update":"u","confidence":0.9}'
        for _ in range(20)
    ]
    llm = make_fake_llm(responses)
    adapter = make_stub_adapter({})  # 无脚本 → 任何工具都会返回 error
    ctx = _StubCtx(llm_client=llm, tool_adapter=adapter)

    result = await run_incremental(state, ctx=ctx)

    # budget 耗尽 → success=False + fallback_reason
    assert result.success is False
    assert result.fallback_reason is not None
    # modified_section_ids 可能空(没成功) — 允许 []