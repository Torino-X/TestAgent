"""test_prep_kb_not_configured — KB 未配置 → KNOWLEDGE_SKIPPED + 优雅 finish (Phase 2.3 §9.4)."""

from __future__ import annotations

import asyncio

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation
from app.agent_runtime.preparation.schemas import AgentDecision

from .conftest import call_kb_decision, kb_envelope_disabled


@pytest.mark.asyncio
async def test_disabled_by_config_emits_knowledge_skipped(
    monkeypatch, fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink
):
    """adapter envelope 标记 disabled_by_config → agent_loop 优雅 finish。"""
    async def _decide(**_kwargs):
        return AgentDecision.model_validate_json(call_kb_decision(query="anything"))

    monkeypatch.setattr(
        "app.agent_runtime.preparation.agent_loop._llm_decide", _decide
    )

    stub_adapter.envelopes_kb = [
        kb_envelope_disabled(reason="KB feature flag disabled")
    ]

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # 优雅降级 — information_sufficient = True (主图不卡)
    assert result.information_sufficient is False
    assert result.knowledge_search_used is True  # 调过工具
    assert result.fallback_reason is None  # 不是 fallback
    assert result.queries == ["anything"]

    await asyncio.sleep(0.05)
    events = in_memory_sink.collect()
    types = [e["event_type"] for e in events]
    assert "knowledge_skipped" in types

    # 找到 knowledge_skipped 事件的 payload
    skipped = next(e for e in events if e["event_type"] == "knowledge_skipped")
    payload = skipped["payload"]
    assert "KB feature flag disabled" in payload.get("reason", "")
    assert payload.get("headline") == "知识库检索跳过"


@pytest.mark.asyncio
async def test_disabled_kb_does_not_set_fallback_reason(
    monkeypatch, fake_llm, stub_adapter, runtime_ctx, base_state
):
    """KB 未配置 ≠ 失败, fallback_reason 必须 None。"""
    async def _decide(**_kwargs):
        return AgentDecision.model_validate_json(call_kb_decision(query="q"))

    monkeypatch.setattr(
        "app.agent_runtime.preparation.agent_loop._llm_decide", _decide
    )
    stub_adapter.envelopes_kb = [kb_envelope_disabled()]

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.fallback_reason is None
    assert result.information_sufficient is False
    # confidence 不应下降到 fallback 等级
    assert result.confidence >= 0.5


@pytest.mark.asyncio
async def test_graph_owned_dual_rag_defers_company_only_tool_call(
    monkeypatch, fake_llm, stub_adapter, runtime_ctx, base_state,
):
    async def _decide(**_kwargs):
        return AgentDecision.model_validate_json(call_kb_decision(query="支付回调异常规则"))

    monkeypatch.setattr(
        "app.agent_runtime.preparation.agent_loop._llm_decide", _decide
    )
    base_state["dual_rag_orchestration"] = True

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.information_sufficient is False
    assert result.queries == ["支付回调异常规则"]
    assert stub_adapter.calls == []
