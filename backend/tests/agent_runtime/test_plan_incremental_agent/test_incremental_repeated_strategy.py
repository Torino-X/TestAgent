"""Test: 8. 同一 tool 同 args 重复调用 → ToolPermissionGuard fail-fast."""

from __future__ import annotations

import pytest

from app.agent_runtime._shared.permission import ToolPermissionGuard
from app.agent_runtime.incremental.decision_filter import filter_incremental_decision
from app.agent_runtime.incremental.permission import INCREMENTAL_TOOL_WHITELIST
from app.agent_runtime.incremental.schemas import IncrementalDecision

from .conftest import build_modification_scope


def test_repeated_same_args_fails_fast():
    """ToolPermissionGuard 同 (tool, args_signature) 计数 ≥ fail_fast_after 触发
    PermanentPermissionDenied → filter 走 action=fail。"""
    scope = build_modification_scope(
        kind="modify_section", target_section_ids=["s1"], allow_extra_sections=False,
    )
    args = {"section_ids": ["s1"], "issues": []}
    decision = IncrementalDecision.model_validate({
        "action": "call_tool",
        "tool_name": "TestPlanRegenTool",
        "tool_arguments": args,
        "target_section_ids": ["s1"],
        "scope_kind": "modify_section",
        "decision_summary": "x",
        "public_update": "y",
        "confidence": 0.9,
    })
    guard = ToolPermissionGuard(fail_fast_after=2)
    # 第一次 OK
    clean1, blocked1 = filter_incremental_decision(
        decision, guard=guard, scope=scope,
        locked_section_ids=[], whitelist=INCREMENTAL_TOOL_WHITELIST,
    )
    assert clean1.action == "call_tool"
    # 第二次 OK 但计数 +1
    clean2, blocked2 = filter_incremental_decision(
        decision, guard=guard, scope=scope,
        locked_section_ids=[], whitelist=INCREMENTAL_TOOL_WHITELIST,
    )
    # 第三次应 fail-fast
    clean3, blocked3 = filter_incremental_decision(
        decision, guard=guard, scope=scope,
        locked_section_ids=[], whitelist=INCREMENTAL_TOOL_WHITELIST,
    )
    assert clean3.action == "fail"
    assert any(
        b.get("outcome") in ("permanent_denied", "repeated_tool")
        for b in blocked3
    )