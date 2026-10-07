"""Phase 2.4 test #3: Schema issues (kind=json_key_rename) — TestPlanRegenTool 接收 issues 列表。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    _make_review_result,
    call_regen_decision,
    call_review_decision,
    finish_decision,
    regen_envelope_ok,
    review_envelope_passed,
)


@pytest.mark.asyncio
async def test_repair_schema_issues(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """schema 类型 issue → regen + review → passed;tool_arguments 包含 issues 列表。"""
    base_state["review_result"] = _make_review_result(
        ["iss-1"], ["sec-A"], level="failed", kind="json_key_rename"
    )
    fake_llm.extend([
        call_regen_decision(["sec-A"], ["iss-1"], summary="重命名 JSON key"),
        call_review_decision(),
        finish_decision(["iss-1"]),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-A"])],
        "ResultReviewTool": [review_envelope_passed()],
    }

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert result.review_passed is True
    regen_call = next(c for c in stub_adapter.calls if c["tool_name"] == "TestPlanRegenTool")
    assert "issues" in regen_call["inputs"]
    assert regen_call["inputs"]["section_ids"] == ["sec-A"]


@pytest.mark.asyncio
async def test_json_truncation_recovery_uses_single_bulk_regen_without_llm_decision(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """JSON 截断是系统性恢复: 不让 RepairAgent LLM 逐轮挑 3 个章节。"""
    section_ids = [f"sec-{idx}" for idx in range(5)]
    issue_ids = [f"iss-{idx}" for idx in range(5)]
    issues = []
    for issue_id, section_id in zip(issue_ids, section_ids):
        issues.append({
            "issue_id": issue_id,
            "rule_id": "generator_schema_missing_field",
            "kind": "generator_missing_field",
            "severity": "block",
            "section_id": section_id,
            "field_path": f"section.{section_id}.content",
            "message": f"{section_id} 因 JSON 截断缺失",
            "evidence": {
                "field": section_id,
                "section_id": section_id,
                "source_error": "json_truncated",
                "recovery_strategy": "bulk_regenerate_missing_sections",
            },
            "repairable": True,
            "suggested_strategy": "regenerate_section",
        })
    base_state["review_result"] = {
        "level": "failed",
        "review_issues": issues,
        "block_issues": list(issues),
        "summary": "JSON 截断导致多个章节缺失",
    }
    base_state["test_plan_content"] = {
        "section_package": {"generated_sections": []},
        "schema_issues": [
            {
                "kind": "missing_field",
                "field": section_id,
                "section_id": section_id,
                "severity": "block",
                "source_error": "json_truncated",
                "recovery_strategy": "bulk_regenerate_missing_sections",
            }
            for section_id in section_ids
        ],
        "generation_recovery": {
            "kind": "json_truncated",
            "mode": "complete",
            "missing_fields": list(section_ids),
            "missing_section_ids": list(section_ids),
            "missing_count": len(section_ids),
            "requires_bulk_repair": True,
        },
    }
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(section_ids, regen_count=len(section_ids))],
        "ResultReviewTool": [review_envelope_passed()],
    }

    result = await run_repair(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.review_passed is True
    assert fake_llm.calls == []
    assert [c["tool_name"] for c in stub_adapter.calls] == [
        "TestPlanRegenTool",
        "ResultReviewTool",
    ]
    regen_call = stub_adapter.calls[0]
    assert regen_call["inputs"]["recovery_mode"] == "json_truncated"
    assert regen_call["inputs"]["bulk_repair"] is True
    assert regen_call["inputs"]["section_ids"] == section_ids


@pytest.mark.asyncio
async def test_json_truncation_recovery_reconstructs_meta_from_schema_issues(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """即便 generation_recovery 被旧 checkpoint/normalize 丢失，也不能退回 3 章普通修复。"""
    section_ids = [f"sec-{idx}" for idx in range(6)]
    issues = [
        {
            "issue_id": f"iss-{idx}",
            "rule_id": "generator_schema_missing_field",
            "kind": "generator_missing_field",
            "severity": "block",
            "section_id": section_id,
            "field_path": f"section.{section_id}.content",
            "message": f"{section_id} 因 JSON 截断缺失",
            "evidence": {
                "field": section_id,
                "section_id": section_id,
                "source_error": "json_truncated",
                "recovery_strategy": "bulk_regenerate_missing_sections",
            },
            "repairable": True,
            "suggested_strategy": "regenerate_section",
        }
        for idx, section_id in enumerate(section_ids)
    ]
    base_state["review_result"] = {
        "level": "failed",
        "review_issues": issues,
        "block_issues": list(issues),
        "summary": "JSON 截断导致多个章节缺失",
    }
    base_state["test_plan_content"] = {
        "section_package": {"generated_sections": []},
        "schema_issues": [
            {
                "kind": "missing_field",
                "field": section_id,
                "section_id": section_id,
                "severity": "block",
                "source_error": "json_truncated",
                "recovery_strategy": "bulk_regenerate_missing_sections",
            }
            for section_id in section_ids
        ],
    }
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(section_ids, regen_count=len(section_ids))],
        "ResultReviewTool": [review_envelope_passed()],
    }

    result = await run_repair(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.review_passed is True
    assert fake_llm.calls == []
    regen_call = stub_adapter.calls[0]
    assert regen_call["tool_name"] == "TestPlanRegenTool"
    assert regen_call["inputs"]["recovery_mode"] == "json_truncated"
    assert regen_call["inputs"]["bulk_repair"] is True
    assert regen_call["inputs"]["section_ids"] == section_ids
