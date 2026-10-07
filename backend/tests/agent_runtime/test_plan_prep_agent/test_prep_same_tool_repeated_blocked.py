"""test_prep_same_tool_repeated_blocked — 同 (tool, sig) 重复 3 次被拦 (Phase 2.3 §9.10)."""

from __future__ import annotations

import json

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation
from app.agent_runtime.preparation.permission import (
    ToolPermissionGuard,
    PermanentPermissionDenied,
)

from .conftest import finish_decision, kb_envelope_ok


def _same_kb_decision(query: str = "永远同 query") -> str:
    return json.dumps(
        {
            "action": "call_tool",
            "tool_name": "KnowledgeSearchTool",
            "tool_arguments": {"query": query, "top_k": 3},
            "decision_summary": f"调 KB: {query}",
            "public_update": "检索",
            "expected_result": None,
            "confidence": 0.5,
        },
        ensure_ascii=False,
    )


def test_guard_blocks_third_repeat():
    """同 (tool, sig) 第 3 次被拦截 (max_repeat=2)。"""
    from app.agent_runtime.preparation.permission import ToolPermissionDenied

    g = ToolPermissionGuard(fail_fast_after=2)
    sig = "abc123def456"  # 任意 12 char

    # 第 1 次:通过
    g.authorize("KnowledgeSearchTool", sig)
    # 第 2 次:通过
    g.authorize("KnowledgeSearchTool", sig)
    # 第 3 次:被拒 (ToolPermissionDenied, repeat limit)
    with pytest.raises(ToolPermissionDenied):
        g.authorize("KnowledgeSearchTool", sig)


def test_guard_fail_fast_after_two_violations():
    """fail_fast_after=2:第 2 次 violation 永久拒绝。"""
    g = ToolPermissionGuard(fail_fast_after=2)

    # 1st violation
    with pytest.raises(Exception):
        g.authorize("BadTool1", "sig1")
    assert not g.is_permanently_denied()

    # 2nd violation → permanently denied
    with pytest.raises(PermanentPermissionDenied):
        g.authorize("BadTool2", "sig2")
    assert g.is_permanently_denied()


def test_guard_different_args_have_independent_counters():
    """不同 args_signature 计数独立。"""
    from app.agent_runtime.preparation.permission import ToolPermissionDenied

    g = ToolPermissionGuard()
    g.authorize("KnowledgeSearchTool", "sig-aaa")
    g.authorize("KnowledgeSearchTool", "sig-bbb")
    g.authorize("KnowledgeSearchTool", "sig-aaa")  # sig-aaa 第 2 次, OK
    # 第 3 次同 sig-aaa 拒绝
    with pytest.raises(ToolPermissionDenied):
        g.authorize("KnowledgeSearchTool", "sig-aaa")


@pytest.mark.asyncio
async def test_same_query_three_calls_results_in_fallback(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """3 次同 query 调 KB → 第 3 次被 guard 拦截 → fallback。"""
    q = "repeating query"
    fake_llm.push(_same_kb_decision(q))
    fake_llm.push(_same_kb_decision(q))
    fake_llm.push(_same_kb_decision(q))

    stub_adapter.envelopes_kb = [
        kb_envelope_ok([{"text": "result", "title": "x", "source": "kb"}]),
        kb_envelope_ok([{"text": "result", "title": "x", "source": "kb"}]),
        # 第 3 次不再消耗 (filter 已 fail)
    ]

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # 第 1-2 次真正调了 adapter,第 3 次被 filter 转 fail → fallback
    assert len(stub_adapter.calls) == 2
    assert result.fallback_reason == "tool_permission_denied"