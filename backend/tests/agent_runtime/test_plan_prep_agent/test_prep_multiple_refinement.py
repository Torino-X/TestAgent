"""test_prep_multiple_refinement — 多次 KB 不同 query refinement (Phase 2.3 §9.3)."""

from __future__ import annotations

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation

from .conftest import call_kb_decision, finish_decision, kb_envelope_ok


@pytest.mark.asyncio
async def test_two_kb_queries_then_finish(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """LLM: call → call → finish;2 个不同 query,evidence 合并。"""
    fake_llm.push(call_kb_decision(query="登录 接口"))
    fake_llm.push(call_kb_decision(query="密码 强度 校验"))
    fake_llm.push(finish_decision(summary="信息已充足"))

    stub_adapter.envelopes_kb = [
        kb_envelope_ok([{"text": "登录接口...", "title": "a.md", "source": "kb"}]),
        kb_envelope_ok([{"text": "密码强度要求8位以上...", "title": "b.md", "source": "kb"}]),
    ]

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.knowledge_search_used is True
    assert len(stub_adapter.calls) == 2
    assert [c["inputs"]["query"] for c in stub_adapter.calls] == ["登录 接口", "密码 强度 校验"]
    assert result.queries == ["登录 接口", "密码 强度 校验"]
    assert len(result.evidence) == 2


@pytest.mark.asyncio
async def test_three_kb_queries_with_refinement(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """3 次 KB 不同 query (refinement); audit 完整保留。"""
    queries = ["OAuth 流程", "OAuth 异常", "OAuth token 刷新"]
    for q in queries:
        fake_llm.push(call_kb_decision(query=q))
    fake_llm.push(finish_decision(summary="ok"))

    stub_adapter.envelopes_kb = [
        kb_envelope_ok([{"text": f"result for {q}", "title": "doc.md", "source": "kb"}])
        for q in queries
    ]

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert len(stub_adapter.calls) == 3
    assert result.queries == queries
    assert len(result.evidence) == 3
    # budget snapshot
    assert result.budget_state.tool_calls == 3
    assert result.budget_state.steps >= 3


@pytest.mark.asyncio
async def test_evidence_dedup_not_required_but_preserved(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """evidence 不主动去重 — 不同 query 的重复内容都保留 (审计可读)。"""
    fake_llm.push(call_kb_decision(query="q1"))
    fake_llm.push(call_kb_decision(query="q2"))
    fake_llm.push(finish_decision(summary="ok"))

    same_chunk = {"text": "重复内容", "title": "x.md", "source": "kb"}
    stub_adapter.envelopes_kb = [
        kb_envelope_ok([same_chunk]),
        kb_envelope_ok([same_chunk]),
    ]

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # 2 条 evidence (audit 完整, 不去重)
    assert len(result.evidence) == 2
    assert result.evidence[0].snippet == "重复内容"
    assert result.evidence[1].snippet == "重复内容"