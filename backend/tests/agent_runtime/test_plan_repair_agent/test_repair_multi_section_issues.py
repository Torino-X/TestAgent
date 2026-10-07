"""Phase 2.4 test #2: Multi-section issues — 3 个 section 全修,均 ≤ MAX_REPAIR_SCOPE_SECTIONS。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair
from app.agent_runtime.graphs.test_plan.constants import MAX_REPAIR_SCOPE_SECTIONS

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    _make_review_result,
    call_regen_decision,
    call_review_decision,
    finish_decision,
    regen_envelope_ok,
    review_envelope_passed,
)


@pytest.mark.asyncio
async def test_repair_multi_section_issues(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """3 个 section 都 block → LLM 一次性 regen 3 个,review 通过。"""
    base_state["review_result"] = _make_review_result(
        ["iss-1", "iss-2", "iss-3"], ["sec-A", "sec-B", "sec-C"], level="failed"
    )

    fake_llm.extend([
        call_regen_decision(["sec-A", "sec-B", "sec-C"],
                            ["iss-1", "iss-2", "iss-3"], summary="批量修复"),
        call_review_decision(summary="复审全部"),
        finish_decision(["iss-1", "iss-2", "iss-3"]),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-A", "sec-B", "sec-C"])],
        "ResultReviewTool": [review_envelope_passed()],
    }

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert result.review_passed is True
    assert len(result.issues_resolved) == 3
    assert set(result.modified_section_ids) == {"sec-A", "sec-B", "sec-C"}


def test_max_repair_scope_sections_constant():
    """MAX_REPAIR_SCOPE_SECTIONS 必须 ≥ 3 以容纳多 section 场景。"""
    assert MAX_REPAIR_SCOPE_SECTIONS >= 3