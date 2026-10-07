"""test_prep_requirement_gap_ask_user — LLM 返回 ask_user + user_questions (Phase 2.3 §9.6)."""

from __future__ import annotations

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation

from .conftest import ask_user_decision, call_kb_decision, finish_decision


@pytest.mark.asyncio
async def test_ask_user_records_user_questions(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """LLM action=ask_user → result.user_questions 填入 question 字段。"""
    fake_llm.push(ask_user_decision(field="requirement_gap", question="需要补充登录模块的业务背景"))

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.information_sufficient is False  # 信息不足
    assert len(result.user_questions) == 1
    q = result.user_questions[0]
    assert q.field == "requirement_gap"
    assert "登录模块" in q.question


@pytest.mark.asyncio
async def test_ask_user_does_not_call_tools(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """ask_user 不调任何工具 (audit 只 1 条 decided)。"""
    fake_llm.push(ask_user_decision())

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert stub_adapter.calls == []
    assert result.knowledge_search_used is False


@pytest.mark.asyncio
async def test_ask_user_then_later_kb_query(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """单次 ask_user → 主循环退出,后续脚本不会再被消费。"""
    fake_llm.push(ask_user_decision(question="需要业务背景"))
    fake_llm.push(call_kb_decision(query="后续"))  # 不会调用
    fake_llm.push(finish_decision(summary="unused"))

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert stub_adapter.calls == []
    assert len(result.user_questions) == 1
    # 1 次 LLM 调用后退出
    assert len(fake_llm.calls) == 1


@pytest.mark.asyncio
async def test_ask_user_fallback_to_legacy_when_no_loop_progress(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """ask_user 后主图仍能继续 — confidence 不会过低 (区别于 fail)。"""
    fake_llm.push(ask_user_decision(question="q"))

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.fallback_reason is None
    assert 0.3 <= result.confidence <= 0.7