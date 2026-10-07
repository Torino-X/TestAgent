"""test_prep_schema_invalid_args — 模型参数 schema 校验失败 (Phase 2.3 §9.9)."""

from __future__ import annotations

import json

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation
from app.agent_runtime.preparation.tool_filter import (
    KnowledgeSearchArgs,
    filter_decision_tool_calls,
)
from app.agent_runtime.preparation.schemas import AgentDecision


def _kb_invalid_args_decision(query_value) -> str:
    """构造 KB 调用,但 query 是 int (违反 str 约束)。"""
    return json.dumps(
        {
            "action": "call_tool",
            "tool_name": "KnowledgeSearchTool",
            "tool_arguments": {"query": query_value, "top_k": 3},
            "decision_summary": "test",
            "public_update": "test",
            "expected_result": None,
            "confidence": 0.5,
        },
        ensure_ascii=False,
    )


def test_kb_schema_rejects_int_query():
    """KnowledgeSearchArgs query must be str (min_length=1, max_length=500)."""
    decision = AgentDecision.model_validate_json(_kb_invalid_args_decision(123))
    guard = _make_guard()
    clean, blocked = filter_decision_tool_calls(decision, guard)
    assert clean.action == "fail"
    assert blocked[0]["reason"].startswith("schema_invalid")
    assert blocked[0]["tool"] == "KnowledgeSearchTool"


def test_kb_schema_rejects_empty_query():
    """query 是空字符串 → schema 拒 (min_length=1)。"""
    decision = AgentDecision.model_validate_json(_kb_invalid_args_decision(""))
    guard = _make_guard()
    clean, blocked = filter_decision_tool_calls(decision, guard)
    assert clean.action == "fail"
    assert blocked


def test_kb_schema_accepts_valid_query():
    """合法 query 通过校验。"""
    decision = AgentDecision.model_validate_json(
        json.dumps(
            {
                "action": "call_tool",
                "tool_name": "KnowledgeSearchTool",
                "tool_arguments": {"query": "登录 异常场景", "top_k": 3},
                "decision_summary": "test",
                "public_update": "test",
            },
            ensure_ascii=False,
        )
    )
    guard = _make_guard()
    clean, blocked = filter_decision_tool_calls(decision, guard)
    assert clean.action == "call_tool"  # 仍然 call_tool (校验通过)
    assert blocked == []


def test_kb_schema_rejects_top_k_out_of_range():
    """top_k 必须 1-20 (ge=1, le=20)."""
    decision = AgentDecision.model_validate_json(
        json.dumps(
            {
                "action": "call_tool",
                "tool_name": "KnowledgeSearchTool",
                "tool_arguments": {"query": "q", "top_k": 100},
                "decision_summary": "test",
            },
            ensure_ascii=False,
        )
    )
    guard = _make_guard()
    clean, blocked = filter_decision_tool_calls(decision, guard)
    assert clean.action == "fail"
    assert "schema_invalid" in blocked[0]["reason"]


@pytest.mark.asyncio
async def test_schema_invalid_in_agent_loop_falls_back(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """模型连续 2 次 schema invalid → fallback (fail-fast)."""
    fake_llm.push(_kb_invalid_args_decision(123))  # invalid
    fake_llm.push(_kb_invalid_args_decision("not_int_anymore"))  # 假设同 query 不重

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # 至少一次 fallback
    assert result.fallback_reason == "tool_permission_denied"


def _make_guard():
    from app.agent_runtime.preparation.permission import ToolPermissionGuard
    return ToolPermissionGuard()


def test_extra_fields_rejected():
    """KnowledgeSearchArgs extra=forbid → 多余字段被 Pydantic 拒。"""
    decision = AgentDecision.model_validate_json(
        json.dumps(
            {
                "action": "call_tool",
                "tool_name": "KnowledgeSearchTool",
                "tool_arguments": {"query": "q", "extra_field": "x"},
                "decision_summary": "test",
            },
            ensure_ascii=False,
        )
    )
    guard = _make_guard()
    clean, blocked = filter_decision_tool_calls(decision, guard)
    assert clean.action == "fail"
    assert blocked