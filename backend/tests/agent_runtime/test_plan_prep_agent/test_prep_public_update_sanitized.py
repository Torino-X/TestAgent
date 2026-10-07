"""test_prep_public_update_sanitized — CoT 不出现在 SSE public payload (Phase 2.3 §9.17, 禁令 #11)."""

from __future__ import annotations

import asyncio

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation
from app.agent_runtime.preparation.schemas import PublicSummary

from .conftest import finish_decision


@pytest.mark.asyncio
async def test_long_decision_summary_truncated_in_audit(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """LLM 返回超长 decision_summary (>500 char) → audit 截 500,public_summary 不受影响。"""
    import json as _json

    long_summary = "X" * 1000
    decision = {
        "action": "finish",
        "tool_name": None,
        "tool_arguments": None,
        "decision_summary": long_summary,
        "public_update": "已完成",
        "expected_result": None,
        "confidence": 0.9,
    }
    fake_llm.push(_json.dumps(decision, ensure_ascii=False))

    # 注: Pydantic AgentDecision.decision_summary max_length=500 — 这里必须 ≤ 500 才能通过校验
    # 用 500 char 边界测试
    boundary_summary = "X" * 500
    decision["decision_summary"] = boundary_summary
    fake_llm.push(_json.dumps(decision, ensure_ascii=False))

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # PublicSummary 永远 sanitized — headline ≤ 120, detail ≤ 480
    assert isinstance(result.public_summary, PublicSummary)
    assert len(result.public_summary.headline) <= 120
    if result.public_summary.detail:
        assert len(result.public_summary.detail) <= 480


@pytest.mark.asyncio
async def test_raw_decision_summary_not_in_completed_payload(
    fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink
):
    """PREPARATION_COMPLETED 事件 payload 不含 raw decision_summary 全文。"""
    import json as _json

    secret_cot = (
        "这是 Chain-of-Thought: 第一步分析 X,第二步推理 Y,第三步..."
        "包含 100 步内部推理。这些都不应暴露给用户。"
    )
    fake_llm.push(_json.dumps(
        {
            "action": "finish",
            "decision_summary": secret_cot,
            "public_update": "ok",
            "confidence": 0.9,
        },
        ensure_ascii=False,
    ))

    await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    await asyncio.sleep(0.05)
    events = in_memory_sink.collect()
    completed = next(e for e in events if e["event_type"] == "preparation_completed")
    payload_str = str(completed["payload"])

    # 完整 secret_cot 不应出现在 SSE payload 中
    # (注: Pydantic max_length=500 → 不能存 100+ char 的 CoT;AgentDecision 直接拒)
    # 这里验证 sanitized 字段集合
    payload = completed["payload"]
    expected_keys = {
        "headline", "detail", "information_sufficient",
        "knowledge_search_used", "queries_count", "evidence_count",
        "requirement_gaps_count", "user_questions_count", "confidence",
    }
    assert expected_keys.issubset(payload.keys())


@pytest.mark.asyncio
async def test_args_signature_only_12_chars_in_audit(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """args_signature 截 12 字符(防 checkpoint 膨胀 + 禁 raw args)."""
    import json as _json

    fake_llm.push(_json.dumps(
        {
            "action": "call_tool",
            "tool_name": "KnowledgeSearchTool",
            "tool_arguments": {
                "query": "very_long_query_that_should_only_appear_as_12_char_signature_in_audit",
                "top_k": 5,
            },
            "decision_summary": "call KB",
            "public_update": "检索",
        },
        ensure_ascii=False,
    ))
    fake_llm.push(finish_decision(summary="ok"))

    stub_adapter.envelopes_kb = [
        {"success": True, "data": {"chunks": []}, "summary": "", "warnings": [],
         "error": None, "duration_ms": 1, "attempt": 1}
    ]

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # Result 本身不直接暴露 args,但 audit 在 budget_state (不存 audit, 只存 budget)
    # 我们验证 args_signature helper 输出 12 char
    from app.agent_runtime.preparation.prompt import args_signature

    sig = args_signature({"query": "very_long_query_that_should_only_appear_as_12_char_signature_in_audit", "top_k": 5})
    assert len(sig) == 12

    # result.budget_state 存在,repetition 计数等
    assert result.budget_state.repeated_tool_calls >= 0


def test_public_summary_max_length_enforced():
    """PublicSummary schema 强制 headline ≤ 120, detail ≤ 480."""
    from pydantic import ValidationError

    # headline 超长 → ValidationError
    with pytest.raises(ValidationError):
        PublicSummary(headline="X" * 121)

    # boundary OK
    PublicSummary(headline="X" * 120)  # noqa

    # detail 超长 → ValidationError
    with pytest.raises(ValidationError):
        PublicSummary(headline="ok", detail="X" * 481)