"""Repair 3.0 regression tests for deterministic re-review after regen."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.agent_runtime._shared.summary_facts import build_summary_facts
from app.agent_runtime.graphs.test_plan.versions.v3.routing import (
    NODE_FAIL_TASK,
    NODE_PREPARE_EXPORT,
    NODE_REPAIR_FALLBACK,
    route_after_repair,
)
from app.agent_runtime.repair.agent_loop import run_repair
from app.agent_runtime.repair.schemas import BudgetState, PublicSummary, RepairResult

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    call_regen_decision,
    fail_decision,
    regen_envelope_ok,
    review_envelope_failed,
    review_envelope_passed,
)


@pytest.mark.asyncio
async def test_regen_success_forces_re_review_before_next_decision(
    fake_llm, stub_adapter, runtime_ctx, base_state,
):
    base_state["test_plan_content"]["schema_issues"] = [
        {
            "kind": "schema_mismatch",
            "field": "sec-A",
            "severity": "block",
            "message": "sec-A 旧 schema 问题",
        },
        {
            "kind": "schema_mismatch",
            "field": "sec-C",
            "severity": "block",
            "message": "sec-C 仍需保留",
        },
    ]
    fake_llm.extend([
        call_regen_decision(["sec-A"], ["iss-1"], summary="先修 sec-A"),
        call_regen_decision(["sec-B"], ["iss-2"], summary="再修 sec-B"),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [
            {
                **regen_envelope_ok(["sec-A"]),
                "data": {
                    **regen_envelope_ok(["sec-A"])["data"],
                    "sections": [
                        {"section_id": "sec-A", "content": "重写后的内容 A"}
                    ],
                },
            },
            {
                **regen_envelope_ok(["sec-B"]),
                "data": {
                    **regen_envelope_ok(["sec-B"])["data"],
                    "sections": [
                        {"section_id": "sec-B", "content": "重写后的内容 B"}
                    ],
                },
            },
        ],
        "ResultReviewTool": [
            review_envelope_failed(["iss-2"], ["sec-B"]),
            review_envelope_passed(),
        ],
    }

    result = await run_repair(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.review_passed is True
    assert result.modified_section_ids == ["sec-A", "sec-B"]
    assert [c["tool_name"] for c in stub_adapter.calls] == [
        "TestPlanRegenTool",
        "ResultReviewTool",
        "TestPlanRegenTool",
        "ResultReviewTool",
    ]
    assert base_state["test_plan_content"]["sections"][0]["content"] == "重写后的内容 A"
    assert base_state["test_plan_content"]["sections"][1]["content"] == "重写后的内容 B"
    assert base_state["test_plan_content"]["schema_issues"] == [
        {
            "kind": "schema_mismatch",
            "field": "sec-C",
            "severity": "block",
            "message": "sec-C 仍需保留",
        }
    ]
    assert len(fake_llm.calls) == 2


@pytest.mark.asyncio
async def test_unresolved_budget_path_blocks_export_and_keeps_public_issue_details(
    monkeypatch, fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink,
):
    monkeypatch.setattr(
        "app.agent_runtime.repair.event_emitter.get_feature_flags",
        lambda: SimpleNamespace(phase29b_narrative_enabled_for=lambda _name: True),
    )
    base_state["review_result"]["review_issues"][0]["message"] = "表头仍不符合模板"
    base_state["review_result"]["block_issues"][0]["message"] = "表头仍不符合模板"
    fake_llm.extend([
        call_regen_decision(["sec-A"], ["iss-1"], summary="尝试修复"),
        fail_decision("预算内无法继续"),
    ])
    failed_review = review_envelope_failed(["iss-1"], ["sec-A"])
    long_message = "用户选择 AI 生成的章节缺失或内容为空，" * 12
    failed_review["data"]["review_issues"][0]["message"] = long_message
    failed_review["data"]["block_issues"][0]["message"] = long_message
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-A"])],
        "ResultReviewTool": [failed_review],
    }

    result = await run_repair(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.review_passed is False
    assert result.remaining_issue_details[0]["section_id"] == "sec-A"
    observation_text = "\n".join(
        e.get("content", "")
        for e in in_memory_sink.events
        if e.get("event_type") == "agent_observation_update"
    )
    assert "**sec-A**" in observation_text
    assert all(
        len(e.get("content", "")) <= 200
        for e in in_memory_sink.events
        if e.get("event_type") == "agent_observation_update"
    )

    state = {
        "repair_result": result.model_dump(),
        "repair_fallback_reason": "budget_exhausted:max_steps",
        "review_result": {
            "level": "failed",
            "repair_issues_remaining": result.issues_remaining,
            "repair_unresolved_issue_details": result.remaining_issue_details,
        },
    }
    assert route_after_repair(state) == NODE_FAIL_TASK
    facts = build_summary_facts(state)
    assert "**sec-A**" in facts["review"]["block_details"][0]


def test_recoverable_context_selection_failure_enters_regeneration_fallback():
    """Required-context selection failure is recoverable, not task-terminal."""
    state = {
        "task_status": "failed",
        "repair_result": {"review_passed": False},
        "repair_fallback_reason": "context_selection_required_unmet:current_goal",
    }

    assert route_after_repair(state) == NODE_REPAIR_FALLBACK


@pytest.mark.asyncio
async def test_repair_subgraph_node_keeps_clean_rereview_result_after_pass(
    monkeypatch, runtime_ctx, base_state,
):
    """复审通过后,主图写回不能重新带出进入 Repair 前的旧 block issue。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
        repair_subgraph_node,
    )

    class MigOffResolver:
        def evaluate(self, flag_name: str) -> bool:
            return False

    async def fake_run_repair_subgraph(state, *, llm_client, tool_adapter, ctx):
        state["review_result"] = {
            "level": "passed",
            "passed": True,
            "review_issues": [],
            "block_issues": [],
            "issues": [],
            "summary": "复审通过",
        }
        return RepairResult(
            review_passed=True,
            issues_resolved=["iss-1", "iss-2"],
            issues_remaining=[],
            remaining_issue_details=[],
            tool_calls_used=2,
            rounds_used=1,
            modified_section_ids=["body_67_level_1"],
            knowledge_evidence=[],
            confidence=1.0,
            public_summary=PublicSummary(headline="修复完成", detail=None),
            fallback_reason=None,
            budget_state=BudgetState(
                steps=1,
                tool_calls=2,
                wall_seconds=1.0,
                token_estimate=0,
                repeated_tool_calls=0,
            ),
        )

    monkeypatch.setattr(
        "app.agent_runtime.repair.subgraph.run_repair_subgraph",
        fake_run_repair_subgraph,
    )
    base_state["review_result"]["review_issues"] = [
        {
            "issue_id": "missing_required_ai_section:body_67_level_1",
            "rule_id": "missing_required_ai_section",
            "kind": "missing_required_ai_section",
            "severity": "block",
            "section_id": "body_67_level_1",
            "message": "用户选择 AI 生成的章节缺失或内容为空",
            "repairable": True,
        },
        {
            "issue_id": "empty_ai_section:section_14",
            "rule_id": "empty_ai_section",
            "kind": "empty_ai_section",
            "severity": "block",
            "section_id": "section_14",
            "message": "生成内容为空",
            "repairable": True,
        },
    ]
    base_state["review_result"]["block_issues"] = list(
        base_state["review_result"]["review_issues"]
    )

    ctx = replace(
        runtime_ctx,
        llm_client=object(),
        task_flag_resolver=MigOffResolver(),
    )

    patch = await repair_subgraph_node(base_state, ctx=ctx)
    review = patch["review_result"]
    facts = build_summary_facts({**base_state, **patch})

    assert review["level"] == "passed"
    assert review["review_issues"] == []
    assert review["block_issues"] == []
    assert review["repair_issues_remaining"] == []
    assert review["repair_unresolved_issue_details"] == []
    assert facts["review"]["block_count"] == 0
