"""Test: 5. locked_section_ids 守门:LLM 不能改 locked sections."""

from __future__ import annotations

import pytest

from app.agent_runtime._shared.permission import ToolPermissionGuard
from app.agent_runtime.incremental.decision_filter import filter_incremental_decision
from app.agent_runtime.incremental.permission import INCREMENTAL_TOOL_WHITELIST
from app.agent_runtime.incremental.scope_guard import ScopeGuardViolation

from .conftest import build_intent, build_modification_scope


def _decision_dict(action="call_tool", tool="TestPlanRegenTool", section_ids=None):
    return {
        "action": action,
        "tool_name": tool,
        "tool_arguments": {"section_ids": section_ids or ["s1"], "issues": []},
        "target_section_ids": section_ids or ["s1"],
        "scope_kind": "modify_section",
        "decision_summary": "x",
        "public_update": "y",
        "confidence": 0.9,
    }


def test_locked_section_blocked_by_scope_guard():
    intent = build_intent(target_section_ids=["s1"], locked_section_ids=["s1"])
    scope = build_modification_scope(
        kind="modify_section", target_section_ids=["s1"], locked_section_ids=["s1"],
    )
    decision = _decision_dict(section_ids=["s1"])

    # Construct IncrementalDecision via model_validate(避开 conftest 间接 import)
    from app.agent_runtime.incremental.schemas import IncrementalDecision
    dec_obj = IncrementalDecision.model_validate(decision)

    guard = ToolPermissionGuard(
        whitelist=INCREMENTAL_TOOL_WHITELIST, fail_fast_after=2,
    )
    clean, blocked = filter_incremental_decision(
        dec_obj, guard=guard, scope=scope,
        locked_section_ids=["s1"], whitelist=INCREMENTAL_TOOL_WHITELIST,
    )

    # scope_guard 抛 ScopeGuardViolation → filter 返回 action=fail
    assert clean.action == "fail"
    assert any(b.get("outcome") == "scope_guard_violation" for b in blocked)


def test_locked_section_not_in_target_passes():
    intent = build_intent(target_section_ids=["s2"], locked_section_ids=["s1"])
    scope = build_modification_scope(
        kind="modify_section", target_section_ids=["s2"], locked_section_ids=["s1"],
    )
    from app.agent_runtime.incremental.schemas import IncrementalDecision
    decision = _decision_dict(section_ids=["s2"])
    dec_obj = IncrementalDecision.model_validate(decision)

    guard = ToolPermissionGuard(
        whitelist=INCREMENTAL_TOOL_WHITELIST, fail_fast_after=2,
    )
    clean, blocked = filter_incremental_decision(
        dec_obj, guard=guard, scope=scope,
        locked_section_ids=["s1"], whitelist=INCREMENTAL_TOOL_WHITELIST,
    )

    assert clean.action == "call_tool"
    assert blocked == []