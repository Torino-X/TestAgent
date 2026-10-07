"""Test: 14. Cancel 路径 — cancellation_service.is_cancelled=True → run_incremental 优雅退出。"""

from __future__ import annotations

import pytest

from app.agent_runtime.incremental.agent_loop import run_incremental

from .conftest import build_intent, build_state


class _StubCtx:
    def __init__(self, llm_client, tool_adapter, cancel_service):
        self.llm_client = llm_client
        self.tool_adapter = tool_adapter
        self.event_sink = _NullSink()
        self.session_factory = lambda: _NullCM()
        self.cancellation_service = cancel_service


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


class _CancelAfterFirst:
    def __init__(self):
        self.calls = 0

    def is_cancelled(self, task_id):
        self.calls += 1
        return self.calls >= 2  # 第一次 False,第二次起 True


@pytest.mark.asyncio
async def test_cancel_triggers_graceful_exit(make_fake_llm, make_stub_adapter):
    """cancel 已标记 → run_incremental 永不抛;fast-fail 路径下
    返回 success=False + fallback_reason。"""
    intent = build_intent(target_section_ids=["s1"], kind="modify_section")
    state = build_state(intent=intent)

    # LLM 总是返回 fail(配合 cancel 不需要再走 tool 路径)
    responses = [
        '{"action":"fail","decision_summary":"cancelled by user",'
        '"public_update":"c","confidence":0.0}'
        for _ in range(3)
    ]
    llm = make_fake_llm(responses)
    adapter = make_stub_adapter({})
    cancel = _CancelAfterFirst()
    ctx = _StubCtx(llm_client=llm, tool_adapter=adapter, cancel_service=cancel)

    result = await run_incremental(state, ctx=ctx)

    # 永不抛
    assert result.success is False
    assert result.fallback_reason is not None
    assert isinstance(result.fallback_reason, str)