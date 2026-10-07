"""test_prep_forbidden_tool_blocked — 白名单外工具被拦截 + fail-fast (Phase 2.3 §9.8)."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation
from app.agent_runtime.preparation.permission import (
    PREPARATION_TOOL_WHITELIST,
    ToolPermissionGuard,
)

from .conftest import finish_decision


def _call_forbidden_tool_decision(tool_name: str = "WordExportTool") -> str:
    return json.dumps(
        {
            "action": "call_tool",
            "tool_name": tool_name,
            "tool_arguments": {"output_path": "/tmp/x.docx"},
            "decision_summary": "尝试调 Word 导出",
            "public_update": "导出 Word",
            "expected_result": "应有文件",
            "confidence": 0.5,
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_forbidden_tool_first_call_fallback(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """LLM 第一次请求 WordExportTool → filter 转 fail + emit PREPARATION_FALLBACK。"""
    fake_llm.push(_call_forbidden_tool_decision("WordExportTool"))

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # adapter 不调
    assert stub_adapter.calls == []
    # fallback 触发
    assert result.fallback_reason == "tool_permission_denied"
    # 1 个 LLM 调用
    assert len(fake_llm.calls) == 1


@pytest.mark.asyncio
async def test_forbidden_tool_emits_preparation_fallback_event(
    fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink
):
    """拦截后 SSE preparation_fallback 事件携带 reason=permission_denied。"""
    fake_llm.push(_call_forbidden_tool_decision("WordExportTool"))

    await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    await asyncio.sleep(0.05)
    events = in_memory_sink.collect()
    types = [e["event_type"] for e in events]
    assert "preparation_fallback" in types

    fallback = next(e for e in events if e["event_type"] == "preparation_fallback")
    assert fallback["payload"]["reason"] == "permission_denied"


@pytest.mark.asyncio
async def test_unknown_tool_falls_back(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """模型请求一个完全不存在工具名 → 拦截 + fallback。"""
    fake_llm.push(_call_forbidden_tool_decision("RandomNonExistentTool"))

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.fallback_reason == "tool_permission_denied"


def test_whitelist_only_contains_knowledge_search():
    """PREPARATION_TOOL_WHITELIST 硬限制只 1 个工具 (Phase 2.3 §3.5)."""
    assert PREPARATION_TOOL_WHITELIST == frozenset({"KnowledgeSearchTool"})


def test_guard_rejects_unknown_tool():
    """Unit: ToolPermissionGuard.authorize() raises for non-whitelisted tool."""
    from app.agent_runtime.preparation.permission import ToolPermissionDenied

    g = ToolPermissionGuard()
    with pytest.raises(ToolPermissionDenied):
        g.authorize("WordExportTool", "abcdef123456")