"""Phase 2.4 test #5: KB evidence required — KnowledgeSearchTool 调用合并入 repair_result.knowledge_evidence。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    call_kb_decision,
    call_regen_decision,
    call_review_decision,
    finish_decision,
    kb_envelope_ok,
    regen_envelope_ok,
    review_envelope_passed,
)


@pytest.mark.asyncio
async def test_repair_kb_evidence_required(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """LLM 调 KB 1 次再 regen。knowledge_evidence 合并入 RepairResult。"""
    fake_llm.extend([
        call_kb_decision("forbidden_pattern best practices", summary="检索规范"),
        call_regen_decision(["sec-A"], ["iss-1"], summary="按规范重写"),
        call_review_decision(),
        finish_decision(["iss-1"]),
    ])
    stub_adapter.envelopes_per_tool = {
        "KnowledgeSearchTool": [
            kb_envelope_ok([
                {"text": "forbidden_pattern rule: avoid 'TODO'", "title": "doc-1",
                 "relevance": 0.9},
            ]),
        ],
        "TestPlanRegenTool": [regen_envelope_ok(["sec-A"])],
        "ResultReviewTool": [review_envelope_passed()],
    }

    result = await run_repair(base_state, llm_client=fake_llm,
                              tool_adapter=stub_adapter, ctx=runtime_ctx)
    assert result.review_passed is True
    assert len(result.knowledge_evidence) == 1
    assert "forbidden_pattern" in result.knowledge_evidence[0].snippet
    kb_calls = [c for c in stub_adapter.calls if c["tool_name"] == "KnowledgeSearchTool"]
    assert len(kb_calls) == 1
    assert kb_calls[0]["inputs"]["query"] == "forbidden_pattern best practices"