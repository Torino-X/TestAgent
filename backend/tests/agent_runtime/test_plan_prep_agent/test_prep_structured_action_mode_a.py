"""test_prep_structured_action_mode_a — Mode A 端到端 happy path (Phase 2.3 §9.14)."""

from __future__ import annotations

import asyncio

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation
from app.agent_runtime.preparation.schemas import PublicSummary
from app.llm.task_profiles import (
    LLMParserType,
    PREPARATION_PROFILE,
    get_builtin_profile,
)

from .conftest import call_kb_decision, finish_decision, kb_envelope_ok


def test_preparation_profile_is_json_strict():
    """PREPARATION_PROFILE 必须 JSON_STRICT (Mode A 强约束)。"""
    assert PREPARATION_PROFILE.parser == LLMParserType.JSON_STRICT
    assert PREPARATION_PROFILE.require_json is True
    assert PREPARATION_PROFILE.on_parse_failure.value == "fallback_default"


def test_preparation_profile_in_builtin_registry():
    """profile 必须注册到 BUILTIN_PROFILES。"""
    assert "preparation_agent" in get_builtin_profile("preparation_agent").name
    assert get_builtin_profile("preparation_agent").name == PREPARATION_PROFILE.name


@pytest.mark.asyncio
async def test_mode_a_happy_path_kb_then_finish(
    fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink
):
    """Mode A happy path: KB → KB → finish。public_summary 完整。"""
    fake_llm.push(call_kb_decision(query="登录"))
    fake_llm.push(call_kb_decision(query="鉴权"))
    fake_llm.push(finish_decision(summary="信息已充足,无缺口"))

    stub_adapter.envelopes_kb = [
        kb_envelope_ok([{"text": "登录...", "title": "a", "source": "kb"}]),
        kb_envelope_ok([{"text": "鉴权...", "title": "b", "source": "kb"}]),
    ]

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # PublicSummary 校验通过 (Pydantic 守门)
    assert isinstance(result.public_summary, PublicSummary)
    assert result.public_summary.headline  # 非空
    assert len(result.public_summary.headline) <= 120
    if result.public_summary.detail:
        assert len(result.public_summary.detail) <= 480

    # 状态完整
    assert result.information_sufficient is True
    assert result.knowledge_search_used is True
    assert len(result.evidence) == 2
    assert len(result.queries) == 2
    assert result.confidence > 0.5
    assert result.fallback_reason is None


@pytest.mark.asyncio
async def test_mode_a_emits_preparation_completed_sanitized(
    fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink
):
    """PREPARATION_COMPLETED 事件 payload sanitized (无 raw CoT / decision_summary 全量)。"""
    fake_llm.push(finish_decision(summary="ok"))

    await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    await asyncio.sleep(0.05)
    events = in_memory_sink.collect()
    completed = next(e for e in events if e["event_type"] == "preparation_completed")

    payload = completed["payload"]
    # 必填 sanitized 字段
    assert "headline" in payload
    assert "information_sufficient" in payload
    assert "queries_count" in payload
    assert "evidence_count" in payload
    # 不应泄漏 raw decision_summary / raw_text / tool_args / CoT
    forbidden_keys = {"decision_summary", "raw_text", "tool_arguments", "tool_name", "candidates"}
    leaked = forbidden_keys & set(payload.keys())
    assert not leaked, f"payload 泄漏内部字段: {leaked}"