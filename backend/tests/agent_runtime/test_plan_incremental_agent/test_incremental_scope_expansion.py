"""Test: 6. scope expansion guard:target ⊆ scope.target_section_ids."""

from __future__ import annotations

import pytest

from app.agent_runtime._shared.permission import ToolPermissionGuard
from app.agent_runtime.incremental.decision_filter import filter_incremental_decision
from app.agent_runtime.incremental.permission import INCREMENTAL_TOOL_WHITELIST
from app.agent_runtime.incremental.schemas import IncrementalDecision
from app.agent_runtime.incremental.scope_guard import (
    MAX_INCREMENTAL_TARGET_SECTIONS,
    enforce_minimal_scope,
    ScopeGuardViolation,
)

from .conftest import build_modification_scope


def test_target_not_in_scope_blocked():
    scope = build_modification_scope(
        kind="modify_section", target_section_ids=["s1"], allow_extra_sections=False,
    )
    decision = IncrementalDecision.model_validate({
        "action": "call_tool",
        "tool_name": "TestPlanRegenTool",
        "tool_arguments": {"section_ids": ["s1", "s2"], "issues": []},
        "target_section_ids": ["s1", "s2"],
        "scope_kind": "modify_section",
        "decision_summary": "x",
        "public_update": "y",
        "confidence": 0.9,
    })

    with pytest.raises(ScopeGuardViolation) as exc_info:
        enforce_minimal_scope(
            decision, scope=scope, locked_section_ids=[],
        )
    assert "extra_sections_not_allowed" in str(exc_info.value)


def test_target_in_scope_passes():
    scope = build_modification_scope(
        kind="modify_section", target_section_ids=["s1", "s2"], allow_extra_sections=False,
    )
    decision = IncrementalDecision.model_validate({
        "action": "call_tool",
        "tool_name": "TestPlanRegenTool",
        "tool_arguments": {"section_ids": ["s1"], "issues": []},
        "target_section_ids": ["s1"],
        "scope_kind": "modify_section",
        "decision_summary": "x",
        "public_update": "y",
        "confidence": 0.9,
    })
    enforce_minimal_scope(decision, scope=scope, locked_section_ids=[])


def test_target_count_exceeds_max_blocked():
    too_many = [f"s{i}" for i in range(MAX_INCREMENTAL_TARGET_SECTIONS + 1)]
    scope = build_modification_scope(
        kind="modify_section",
        target_section_ids=too_many,
        allow_extra_sections=True,
    )
    decision = IncrementalDecision.model_validate({
        "action": "call_tool",
        "tool_name": "TestPlanRegenTool",
        "tool_arguments": {"section_ids": too_many, "issues": []},
        "target_section_ids": too_many,
        "scope_kind": "modify_section",
        "decision_summary": "x",
        "public_update": "y",
        "confidence": 0.9,
    })
    with pytest.raises(ScopeGuardViolation):
        enforce_minimal_scope(
            decision, scope=scope, locked_section_ids=[],
        )


def test_scope_kind_mismatch_blocked():
    scope = build_modification_scope(kind="re_review", target_section_ids=[])
    decision = IncrementalDecision.model_validate({
        "action": "call_tool",
        "tool_name": "ResultReviewTool",
        "tool_arguments": {},
        "target_section_ids": [],
        "scope_kind": "modify_section",  # wrong!
        "decision_summary": "x",
        "public_update": "y",
        "confidence": 0.9,
    })
    with pytest.raises(ScopeGuardViolation):
        enforce_minimal_scope(
            decision, scope=scope, locked_section_ids=[],
        )


def test_filter_decision_returns_fail_on_scope_violation():
    scope = build_modification_scope(
        kind="modify_section", target_section_ids=["s1"], allow_extra_sections=False,
    )
    decision = IncrementalDecision.model_validate({
        "action": "call_tool",
        "tool_name": "TestPlanRegenTool",
        "tool_arguments": {"section_ids": ["s1", "s9"], "issues": []},
        "target_section_ids": ["s1", "s9"],
        "scope_kind": "modify_section",
        "decision_summary": "x",
        "public_update": "y",
        "confidence": 0.9,
    })
    guard = ToolPermissionGuard(fail_fast_after=2)
    clean, blocked = filter_incremental_decision(
        decision, guard=guard, scope=scope,
        locked_section_ids=[], whitelist=INCREMENTAL_TOOL_WHITELIST,
    )
    assert clean.action == "fail"
    assert any(b.get("outcome") == "scope_guard_violation" for b in blocked)