"""test_prep_no_kb_needed — LLM 立即 finish,无 KB 调用 (Phase 2.3 §9.1)."""

from __future__ import annotations

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation

from .conftest import finish_decision


@pytest.mark.asyncio
async def test_llm_first_response_finish_no_kb_called(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """LLM 单步返回 finish → run_preparation 直接返回,未调任何工具。"""
    fake_llm.push(finish_decision(summary="信息已充足,无需 KB"))

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # result shape
    assert result.information_sufficient is True
    assert result.knowledge_search_used is False
    assert result.fallback_reason is None
    assert len(result.queries) == 0
    assert len(result.evidence) == 0

    # tool adapter 一次未调
    assert stub_adapter.calls == []

    # 审计:1 条 decided(finish) — tool_calls = 0,≥ 0 通过
    assert result.budget_state.tool_calls >= 0
    # 仅 1 个 LLM 调用
    assert len(fake_llm.calls) == 1


@pytest.mark.asyncio
async def test_first_response_decision_summary_recorded(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """finish 决策的 decision_summary 不出现在 SSE public_summary 中。"""
    fake_llm.push(finish_decision(summary="信息已充足"))

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # PublicSummary 是 sanitized — headline 字段,不含 CoT
    assert result.public_summary.headline
    assert result.public_summary.detail is not None
    # 内部 decision_summary 不在 public payload 中
    assert "信息已充足" not in result.public_summary.headline  # 仅 headline + detail sanitized


@pytest.mark.asyncio
async def test_no_kb_emits_knowledge_skipped_not(
    fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink
):
    """finish(无 KB)不触发 KNOWLEDGE_SKIPPED — 仅 PREPARATION_STARTED + COMPLETED。"""
    fake_llm.push(finish_decision())

    await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # 同步收集需要稍等 create_task;用 asyncio.sleep 让事件落定
    import asyncio
    await asyncio.sleep(0.05)

    events = in_memory_sink.collect()
    event_types = [e["event_type"] for e in events]
    assert "preparation_started" in event_types
    assert "preparation_completed" in event_types
    # 没有 KB 相关事件
    assert "knowledge_skipped" not in event_types
    assert "knowledge_insufficient" not in event_types