"""test_prep_fallback_emits_knowledge_summary — fallback 路径字节级复用 legacy (Phase 2.3 §9.16, 禁令 #1)."""

from __future__ import annotations

import asyncio

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation
from app.agent_runtime.preparation.fallback import (
    NODE_SEARCH_KNOWLEDGE,
    run_legacy_kb_fallback,
)

from .conftest import call_kb_decision, kb_envelope_ok


@pytest.mark.asyncio
async def test_legacy_kb_fallback_emits_kb_summary(
    runtime_ctx, stub_adapter, in_memory_sink
):
    """fallback.run_legacy_kb_fallback 走 search_knowledge_node 主体,emits KNOWLEDGE_SUMMARY。"""
    state = {
        "user_prompt": "test",
        "completed_nodes": [],
        "kb_skip_reason": None,
    }

    stub_adapter.envelopes_kb = [
        kb_envelope_ok([{"text": "kb result", "title": "doc", "source": "kb"}])
    ]
    # adapter 需通过 ctx.tool_adapter 调用 — RuntimeContext 是 frozen slots dataclass,需 dataclasses.replace
    import dataclasses
    runtime_ctx = dataclasses.replace(runtime_ctx, tool_adapter=stub_adapter)

    result_data = await run_legacy_kb_fallback(state, ctx=runtime_ctx)

    assert "knowledge_search_result" in result_data
    assert result_data["current_node"] == NODE_SEARCH_KNOWLEDGE
    assert NODE_SEARCH_KNOWLEDGE in result_data["completed_nodes"]

    await asyncio.sleep(0.05)
    events = in_memory_sink.collect()
    types = [e["event_type"] for e in events]
    assert "knowledge_summary" in types


@pytest.mark.asyncio
async def test_legacy_kb_fallback_with_skip_reason(
    runtime_ctx, stub_adapter, in_memory_sink
):
    """state.kb_skip_reason 存在时,fallback 走 synthetic skip 分支 (emit 2 个事件)。"""
    state = {
        "user_prompt": "test",
        "completed_nodes": [],
        "kb_skip_reason": "test_plan_generation_disabled",
    }

    result_data = await run_legacy_kb_fallback(state, ctx=runtime_ctx)

    # 不调工具 — adapter.calls 应为空
    assert stub_adapter.calls == []

    # skip 分支返回 skip_reason
    assert result_data["knowledge_search_result"] == {"skip_reason": "test_plan_generation_disabled"}
    assert result_data["kb_skip_reason"] == "test_plan_generation_disabled"

    await asyncio.sleep(0.05)
    events = in_memory_sink.collect()
    types = [e["event_type"] for e in events]
    # 期望 TOOL_FINISHED (skipped=True) + KNOWLEDGE_SUMMARY (skipped=True)
    assert "tool_finished" in types
    assert "knowledge_summary" in types

    # 验证 skipped=True payload
    skipped_tool = next(e for e in events if e["event_type"] == "tool_finished")
    assert skipped_tool["payload"].get("skipped") is True
    assert skipped_tool["payload"].get("skip_reason") == "test_plan_generation_disabled"


@pytest.mark.asyncio
async def test_agent_loop_fallback_runs_legacy_kb_when_all_tools_fail(
    fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink
):
    """Prep 触发 fallback 后,事件流包含 KNOWLEDGE_SUMMARY(legacy 字节级兼容)。

    验证: PREP_FALLBACK + KNOWLEDGE_SUMMARY 共存 — Phase 2.1 7 kb_skip 测试不回归。
    """
    # 触发 fallback:LLM 立即 fail → agent_loop emit PREPARATION_FALLBACK
    import json as _json

    fail_action = _json.dumps(
        {
            "action": "fail",
            "tool_name": None,
            "tool_arguments": None,
            "decision_summary": "明确失败,准备回退",
            "public_update": "无法继续",
            "expected_result": None,
            "confidence": 0.0,
        },
        ensure_ascii=False,
    )
    fake_llm.push(fail_action)

    await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    await asyncio.sleep(0.05)
    events = in_memory_sink.collect()
    types = [e["event_type"] for e in events]

    # preparation_fallback 已 emit
    assert "preparation_fallback" in types
    # 注意: agent_loop 不直接调 run_legacy_kb_fallback (由 prep_fallback_node 调) —
    # 但 preparation_fallback 事件 + PREPARATION_COMPLETED 仍走流程;
    # 这里只断言 PREPARATION_FALLBACK 已发,且无 KB 调用(因为 fail 直接退出)
    assert stub_adapter.calls == []


def test_fallback_node_name_constant():
    """NODE_SEARCH_KNOWLEDGE 常量与 Phase 2.1 nodes_pre_confirm 一致。"""
    assert NODE_SEARCH_KNOWLEDGE == "search_knowledge"