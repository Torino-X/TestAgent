"""Test: 17. JSON 输出契约 — IncrementalDecision / RepairResult / PublicSummary 的 Pydantic 校验。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent_runtime.incremental.schemas import (
    IncrementalDecision,
    PublicSummary,
)


def test_incremental_decision_valid_call_tool():
    dec = IncrementalDecision.model_validate({
        "action": "call_tool",
        "tool_name": "TestPlanRegenTool",
        "tool_arguments": {"section_ids": ["s1"]},
        "target_section_ids": ["s1"],
        "scope_kind": "modify_section",
        "decision_summary": "ok",
        "public_update": "u",
        "confidence": 0.9,
    })
    assert dec.action == "call_tool"


def test_incremental_decision_action_finish():
    dec = IncrementalDecision.model_validate({
        "action": "finish",
        "decision_summary": "done",
        "public_update": "complete",
        "confidence": 0.8,
    })
    assert dec.action == "finish"


def test_incremental_decision_invalid_action_rejected():
    with pytest.raises(ValidationError):
        IncrementalDecision.model_validate({
            "action": "invalid_action",
            "decision_summary": "x",
            "public_update": "y",
        })


def test_public_summary_length_capped():
    """headline ≤ 120 / detail ≤ 480 防 CoT 膨胀。"""
    with pytest.raises(ValidationError):
        PublicSummary(headline="x" * 200, detail="ok")
    with pytest.raises(ValidationError):
        PublicSummary(headline="ok", detail="x" * 500)


def test_public_summary_accepts_valid_lengths():
    s = PublicSummary(headline="x" * 120, detail="y" * 480)
    assert len(s.headline) == 120
    assert len(s.detail) == 480


def test_incremental_decision_confidence_range():
    with pytest.raises(ValidationError):
        IncrementalDecision.model_validate({
            "action": "finish",
            "decision_summary": "x",
            "public_update": "y",
            "confidence": 1.5,  # > 1
        })
    with pytest.raises(ValidationError):
        IncrementalDecision.model_validate({
            "action": "finish",
            "decision_summary": "x",
            "public_update": "y",
            "confidence": -0.1,  # < 0
        })