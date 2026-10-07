"""Phase 2.1 — Equivalence happy path。

验证 LangGraph v2 走 pre-confirm 时按预期序列发射事件,
并到达 NEED_USER_CONFIRM 暂停点。

Phase 2.1 范围:harness 只驱动 LangGraph 端;Legacy 端真对比留 Phase 2.2。
"""

from __future__ import annotations

import pytest

from app.agent_runtime.graphs.test_plan.constants import GRAPH_VERSION_V2

from .equivalence.scenarios import HAPPY_PATH


@pytest.mark.asyncio
async def test_equivalence_happy_path_pre_confirm():
    """happy path 走 pre-confirm 时,事件序列前缀匹配 expected_prefix,
    且停在 need_user_confirm。"""
    from .equivalence.harness import run_langgraph_pre_confirm

    scenario = HAPPY_PATH
    result = await run_langgraph_pre_confirm(
        task_id="t-equivalent-1",
        user_prompt=scenario.user_prompt,
        requirement_file_id=scenario.requirement_file_id,
        template_file_id=scenario.template_file_id,
        kb_skip_reason=scenario.kb_skip_reason,
        # Phase 2.9A.7: 该测试期望 need_user_confirm 事件序列 — 必须
        # 走 v2 sentinel pause 路径(interrupt_enabled=False)。
        graph_version=GRAPH_VERSION_V2,
    )

    actual_types = [e["event_type"] for e in result.events]
    expected = scenario.expected_prefix

    # 验证 expected_prefix 全部出现在 actual_types 中(按序)
    ai = 0
    for ex in expected:
        while ai < len(actual_types) and actual_types[ai] != ex:
            ai += 1
        assert ai < len(actual_types), (
            f"happy_path: expected event {ex!r} not found in actual sequence\n"
            f"  actual: {actual_types}"
        )
        ai += 1

    # 最终应在 NEED_USER_CONFIRM 暂停点
    assert result.pause_marker == "need_user_confirm", (
        f"expected pause_marker=need_user_confirm, got {result.pause_marker}"
    )


@pytest.mark.asyncio
async def test_equivalence_happy_path_state_snapshot():
    """happy path 终态 state 字段集合应包含 pre-confirm 阶段的全部输出。"""
    from .equivalence.harness import run_langgraph_pre_confirm

    scenario = HAPPY_PATH
    result = await run_langgraph_pre_confirm(
        task_id="t-equivalent-2",
        user_prompt=scenario.user_prompt,
        requirement_file_id=scenario.requirement_file_id,
        template_file_id=scenario.template_file_id,
        kb_skip_reason=scenario.kb_skip_reason,
        # Phase 2.9A.7: 期望 ``pause_for_legacy_confirm`` 在 completed_nodes
        # 中 → 走 v2 sentinel pause 路径。
        graph_version=GRAPH_VERSION_V2,
    )

    expected_fields = {
        "task_id",
        "graph_run_id",
        "graph_version",
        "graph_name",
        "current_phase",
        "current_node",
        "task_status",
        "completed_nodes",
        "pause_marker",
        "requirement_analysis",
        "template_structure",
        "review_standard",
        "knowledge_search_result",
        "section_suggestions",
    }
    actual_fields = set(result.final_state.keys())
    missing = expected_fields - actual_fields
    assert not missing, f"missing state fields: {missing}"
    assert result.final_state["pause_marker"] == "need_user_confirm"
    assert "pause_for_legacy_confirm" in result.final_state["completed_nodes"]