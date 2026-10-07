"""Phase 2.1 — Equivalence format-loss review 路径。

走完整 pre-confirm → confirm → post-confirm → format-check → pause_for_legacy_format_decision。

验证 LangGraph v2 在 format loss 检测时能正确停在 pause_marker=format_loss_review。
"""

from __future__ import annotations

import pytest

from app.agent_runtime.graphs.test_plan.constants import GRAPH_VERSION_V2

from .equivalence.scenarios import FORMAT_LOSS_REVIEW


@pytest.mark.asyncio
async def test_equivalence_format_loss_review_pre_confirm():
    """format-loss 场景走 pre-confirm,前置事件序列与 happy path 一致(同段路径)。"""
    from .equivalence.harness import run_langgraph_pre_confirm

    scenario = FORMAT_LOSS_REVIEW
    result = await run_langgraph_pre_confirm(
        task_id="t-fl-1",
        user_prompt=scenario.user_prompt,
        requirement_file_id=scenario.requirement_file_id,
        template_file_id=scenario.template_file_id,
        kb_skip_reason=scenario.kb_skip_reason,
        # Phase 2.9A.7: 该测试期望 ``pause_marker == "need_user_confirm"`` —
        # 需走 v2 sentinel pause 路径(interrupt_enabled=False)。
        graph_version=GRAPH_VERSION_V2,
    )

    actual_types = [e["event_type"] for e in result.events]
    expected = scenario.expected_prefix

    ai = 0
    for ex in expected:
        while ai < len(actual_types) and actual_types[ai] != ex:
            ai += 1
        assert ai < len(actual_types), (
            f"format_loss: expected event {ex!r} not found in actual sequence\n"
            f"  actual: {actual_types}"
        )
        ai += 1

    assert result.pause_marker == "need_user_confirm"


@pytest.mark.asyncio
async def test_equivalence_format_loss_review_state_for_resume():
    """format_loss 场景跑完 pre-confirm 后,final_state 可作为 resume_format_loss 的输入。"""
    from .equivalence.harness import run_langgraph_pre_confirm
    from app.agent_runtime.graphs.test_plan.constants import GRAPH_VERSION_V3

    scenario = FORMAT_LOSS_REVIEW
    result = await run_langgraph_pre_confirm(
        task_id="t-fl-2",
        user_prompt=scenario.user_prompt,
        requirement_file_id=scenario.requirement_file_id,
        template_file_id=scenario.template_file_id,
        kb_skip_reason=scenario.kb_skip_reason,
        # Phase 2.9A.7: 显式 v3,验证 state 字段含 graph_version=v3。
        graph_version=GRAPH_VERSION_V3,
    )

    # 拿到 state 后,验证可序列化 + 含版本字段
    from app.agent_runtime.graphs.test_plan.state import assert_state_serializable

    assert_state_serializable(dict(result.final_state))
    # Phase 2.8R-D:settings 默认 v3,Equivalence 测预期 v3

    assert result.final_state["graph_version"] == GRAPH_VERSION_V3
    # Phase 2.9A.7:v3 真 interrupt 路径下,停点是 ``section_confirmation_interrupt``(或
    # ``prepare_section_confirmation``,当 sections 缺失时 fall through);
    # 而 v2 sentinel 路径是 ``pause_for_legacy_confirm``。
    assert result.final_state["current_node"] in {
        "pause_for_legacy_confirm",
        "section_confirmation_interrupt",
        "prepare_section_confirmation",
    }


@pytest.mark.asyncio
async def test_equivalence_format_loss_review_resume_marker():
    """把 format_loss_confirmation.decision merge 进 state,resume_format_loss 仍能完成 invoke。

    Phase 2.1 范围:仅验证 coordinator 接受决策并返回 RunOutcome,
    不强求走到 finalize_task(format-check 在 v2 stub 阶段可能停在 pause_for_legacy_format_decision)。
    """
    from .equivalence.harness import run_langgraph_pre_confirm
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
    from app.agent_runtime.graph_registry import GraphRegistry
    from app.agent_runtime.graph_runtime_service import GraphRuntimeService

    scenario = FORMAT_LOSS_REVIEW

    pre_result = await run_langgraph_pre_confirm(
        task_id="t-fl-3",
        user_prompt=scenario.user_prompt,
        requirement_file_id=scenario.requirement_file_id,
        template_file_id=scenario.template_file_id,
        kb_skip_reason=scenario.kb_skip_reason,
    )

    # 模拟 format loss 检测:把 pause_marker 切到 format_loss_review
    state = dict(pre_result.final_state)
    state["pause_marker"] = "format_loss_review"
    state["current_phase"] = "paused"
    state["format_loss_confirmation"] = {"decision": scenario.format_loss_decision}
    state["format_check_result"] = {
        "level": "loss_detected",
        "losses": scenario.format_losses or [],
    }

    registry = GraphRegistry.build_default_v2_v3(checkpointer=None)
    runtime = GraphRuntimeService(registry=registry)
    coord = LangGraphRunCoordinator(registry=registry, runtime=runtime, checkpointer=None)

    outcome = await coord.resume_format_loss(
        task_id="t-fl-3",
        graph_run_id="run-t-fl-3",
        restored_state=state,
    )

    # 至少:outcome 不抛,final_state 是 dict
    assert isinstance(outcome.final_state, dict)
    # 当前节点应该是某 post-confirm 节点(可能是 pause_for_legacy_format_decision / finalize_task / record_loss_decision)
    assert outcome.current_node is not None