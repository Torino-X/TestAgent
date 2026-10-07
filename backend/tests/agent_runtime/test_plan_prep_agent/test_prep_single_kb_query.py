"""test_prep_single_kb_query — 调 KB 1 次,evidence 解析 (Phase 2.3 §9.2)."""

from __future__ import annotations

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation

from .conftest import call_kb_decision, finish_decision, kb_envelope_ok


@pytest.mark.asyncio
async def test_single_kb_query_then_finish(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """LLM: 1) call KB → 2) finish。1 次 KB 调,evidence 进入 result。"""
    fake_llm.push(call_kb_decision(query="登录 接口 异常场景"))
    fake_llm.push(finish_decision(summary="检索后信息已充足"))

    stub_adapter.envelopes_kb = [
        kb_envelope_ok([
            {"text": "登录接口异常场景包括密码错误、账号锁定、token过期等。", "title": "登录异常.md", "source": "internal-wiki"},
            {"text": "建议覆盖正向、负向、边界三类用例。", "title": "测试设计指南.md", "source": "internal-wiki"},
        ])
    ]

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # KB 调了 1 次
    assert len(stub_adapter.calls) == 1
    assert stub_adapter.calls[0]["tool_name"] == "KnowledgeSearchTool"
    assert stub_adapter.calls[0]["inputs"]["query"] == "登录 接口 异常场景"

    # result
    assert result.information_sufficient is True
    assert result.knowledge_search_used is True
    assert result.queries == ["登录 接口 异常场景"]
    assert len(result.evidence) == 2
    assert result.evidence[0].query == "登录 接口 异常场景"
    assert result.evidence[0].snippet.startswith("登录接口异常场景")
    # evidence[0].source: agent_loop 优先取 chunk.get("title"),回退 chunk.get("source")
    assert result.evidence[0].source == "登录异常.md"


@pytest.mark.asyncio
async def test_evidence_snippet_truncated_to_400(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """Evidence.snippet 长度上限 400 字符 (Pydantic schema 守门)。"""
    fake_llm.push(call_kb_decision(query="x"))
    fake_llm.push(finish_decision(summary="ok"))

    huge = "a" * 1000  # 远超 400
    stub_adapter.envelopes_kb = [
        kb_envelope_ok([{"text": huge, "title": "long.md", "source": "kb"}])
    ]

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert len(result.evidence) == 1
    assert len(result.evidence[0].snippet) == 400


@pytest.mark.asyncio
async def test_kb_insufficient_emits_event(
    fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink
):
    """KB 返回 0 chunks → 触发 KNOWLEDGE_INSUFFICIENT → best-effort finish。"""
    fake_llm.push(call_kb_decision(query="obscure_term"))
    # 不需要第二脚本: KB 返回 0 → agent_loop break, 直接 best-effort finish

    stub_adapter.envelopes_kb = [kb_envelope_ok([])]

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert len(result.evidence) == 0
    assert "knowledge_returned_zero_chunks" in (result.constraints[0] if result.constraints else "")

    import asyncio
    await asyncio.sleep(0.05)
    events = in_memory_sink.collect()
    types = [e["event_type"] for e in events]
    assert "knowledge_insufficient" in types