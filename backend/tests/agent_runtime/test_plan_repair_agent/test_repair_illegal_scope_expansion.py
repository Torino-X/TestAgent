"""Phase 2.4 test #7: Illegal scope expansion — LLM 决策超出 review_issues 范围 → scope_guard 拦截 → fail。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    _make_review_result,
    call_regen_decision,
    fail_decision,
)


@pytest.mark.asyncio
async def test_repair_illegal_scope_expansion(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """review_issues 只涉及 sec-A,LLM 决策改 sec-A + sec-B → scope_guard 拦截 → fail。"""
    base_state["review_result"] = _make_review_result(
        ["iss-1"], ["sec-A"], level="failed"
    )

    # LLM 试图扩大范围;scope_guard 应将其改为 fail。
    fake_llm.extend([
        call_regen_decision(["sec-A", "sec-B"], ["iss-1"], summary="扩大范围"),
        fail_decision("scope_guard violation"),
    ])

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert result.review_passed is False
    # scope_guard 触发时 fallback_reason 应反映拦截
    assert result.fallback_reason is not None
    # 关键:sec-B 永远没被修改
    assert "sec-B" not in result.modified_section_ids


def test_scope_guard_rejects_out_of_scope():
    """enforce_minimal_scope 直接校验超范围输入。"""
    from app.agent_runtime.repair.scope_guard import enforce_minimal_scope
    from app.agent_runtime.repair.schemas import RepairDecision, ReviewIssue

    issues = [
        ReviewIssue(
            issue_id="iss-1", rule_id="r1", kind="forbidden_pattern",
            severity="block", section_id="sec-A", field_path="section.sec-A.content",
            message="violation", evidence="…", expected_rule="no forbidden",
            repairable=True, suggested_strategy="regenerate_section",
        ),
    ]
    decision = RepairDecision(
        action="call_tool", tool_name="TestPlanRegenTool",
        tool_arguments={"section_ids": ["sec-A", "sec-B"], "issues": []},
        target_issue_ids=["iss-1"],
        target_section_ids=["sec-A", "sec-B"],
        suggested_strategy="regenerate_section",
        decision_summary="扩大范围",
    )
    with pytest.raises(Exception):
        enforce_minimal_scope(decision, review_issues=issues, locked_section_ids=[],
                              max_scope=3)