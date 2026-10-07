"""test_prep_kb_transient_failure — recoverable failure → PREPARATION_FALLBACK (Phase 2.3 §9.5)."""

from __future__ import annotations

import asyncio

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation

from .conftest import call_kb_decision, kb_envelope_recoverable_failure


@pytest.mark.asyncio
async def test_recoverable_kb_failure_emits_preparation_fallback(
    fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink
):
    """KB envelope.error.recoverable=True → agent_loop emit PREPARATION_FALLBACK。"""
    fake_llm.push(call_kb_decision(query="q"))
    stub_adapter.envelopes_kb = [
        kb_envelope_recoverable_failure(message="上游 KB 服务暂时不可用")
    ]

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # fallback_reason 标记
    assert result.fallback_reason == "tool_recoverable_failure"
    # 不算 information_sufficient (主图仍可走章节确认)
    # confidence 下降
    assert result.confidence <= 0.5

    await asyncio.sleep(0.05)
    events = in_memory_sink.collect()
    types = [e["event_type"] for e in events]
    assert "preparation_fallback" in types


@pytest.mark.asyncio
async def test_non_recoverable_kb_failure_also_falls_back(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """KB envelope.error.recoverable 缺省/False → tool_failure fallback。"""
    fake_llm.push(call_kb_decision(query="q"))

    bad_envelope = {
        "success": False,
        "tool_name": "KnowledgeSearchTool",
        "task_id": "stub",
        "data": {},
        "summary": "",
        "warnings": [],
        "error": {"code": "KB_INTERNAL", "message": "fatal", "recoverable": False},
        "duration_ms": 1,
        "attempt": 1,
    }
    stub_adapter.envelopes_kb = [bad_envelope]

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.fallback_reason == "tool_failure"


@pytest.mark.asyncio
async def test_transient_failure_does_not_crash_main(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """PREPARATION_FALLBACK 是 graceful — 主任务不挂 (Rule 14)。"""
    fake_llm.push(call_kb_decision(query="q"))
    stub_adapter.envelopes_kb = [kb_envelope_recoverable_failure()]

    # 必须返回 PreparationResult (不抛)
    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result is not None
    assert result.public_summary is not None
    assert result.public_summary.headline  # 兜底 headline 不为空