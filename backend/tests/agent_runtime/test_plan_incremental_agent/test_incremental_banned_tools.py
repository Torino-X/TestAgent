"""Test: 7. Banned tools 拦截。"""

from __future__ import annotations

import pytest

from app.agent_runtime._shared.permission import ToolPermissionGuard
from app.agent_runtime.incremental.decision_filter import filter_incremental_decision
from app.agent_runtime.incremental.permission import INCREMENTAL_TOOL_WHITELIST
from app.agent_runtime.incremental.schemas import IncrementalDecision

from .conftest import build_modification_scope


@pytest.mark.parametrize("banned_tool", [
    "TestPlanGeneratorTool",
    "RequirementParserTool",
    "TemplateParserTool",
    "SectionSuggestionTool",
    "UserConfigUpdateTool",
    "ArtifactWriteTool",
    "FileSystemWriteTool",
])
def test_banned_tool_blocked(banned_tool):
    scope = build_modification_scope(
        kind="modify_section", target_section_ids=["s1"], allow_extra_sections=False,
    )
    decision = IncrementalDecision.model_validate({
        "action": "call_tool",
        "tool_name": banned_tool,
        "tool_arguments": {},
        "target_section_ids": [],
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
    assert any(b.get("outcome") == "banned" for b in blocked)