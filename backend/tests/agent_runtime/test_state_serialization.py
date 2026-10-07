"""State 序列化约束。"""

from __future__ import annotations

import pytest

from app.agent_runtime.graphs.test_plan.state import (
    RuntimeContextLeakedIntoState,
    TestPlanGraphState,
    assert_state_serializable,
    make_empty_state,
)


def test_make_empty_state_is_serializable() -> None:
    state = make_empty_state(task_id="t-1", graph_run_id="r-1")
    assert_state_serializable(dict(state))


def test_state_serializable_rejects_callable() -> None:
    state: dict = make_empty_state(task_id="t-1", graph_run_id="r-1")
    state["not_callable"] = lambda: 1
    with pytest.raises(RuntimeContextLeakedIntoState):
        assert_state_serializable(state)


def test_state_serializable_rejects_session_like() -> None:
    """模拟 Session 对象被错误塞入 state。"""

    class FakeSession:
        pass

    state: dict = make_empty_state(task_id="t-1", graph_run_id="r-1")
    state["session"] = FakeSession()
    with pytest.raises(RuntimeContextLeakedIntoState):
        assert_state_serializable(state)


def test_state_total_false_partial_ok() -> None:
    state: TestPlanGraphState = {"task_id": "t"}
    assert state["task_id"] == "t"
    assert "completed_nodes" not in state


# ── Phase 2.1 新字段 ──────────────────────────────────────────────────────


def test_v2_state_extension_default_serializable() -> None:
    """Phase 2.1 state 扩展字段在 graph_version=v2 时默认都存在且可序列化。"""
    from app.agent_runtime.graphs.test_plan.constants import GRAPH_VERSION_V2

    state = make_empty_state(
        task_id="t-1", graph_run_id="r-1", graph_version=GRAPH_VERSION_V2
    )
    assert_state_serializable(dict(state))

    # 必填字段都得是 None 或可序列化
    for field in (
        "requirement_analysis",
        "template_structure",
        "knowledge_search_result",
        "section_suggestions",
        "section_confirm_config",
        "test_plan_content",
        "review_result",
        "review_standard",
        "artifact",
        "format_check_result",
        "pending_format_losses",
        "format_loss_confirmation",
        "kb_skip_reason",
        "pause_marker",
        "last_retry_strategy",
    ):
        assert field in state, f"v2 state missing field: {field}"
        assert state[field] in (None, list, dict, str, int, float, bool), (
            f"v2 field {field}={state[field]!r} not serialized"
        )


def test_v2_state_loop_counters_int() -> None:
    """review_loop_count / format_loop_count 是 int。"""
    from app.agent_runtime.graphs.test_plan.constants import GRAPH_VERSION_V2

    state = make_empty_state(
        task_id="t-1", graph_run_id="r-1", graph_version=GRAPH_VERSION_V2
    )
    assert isinstance(state["review_loop_count"], int)
    assert isinstance(state["format_loop_count"], int)
    assert isinstance(state["format_loss_timeout_seconds"], int)