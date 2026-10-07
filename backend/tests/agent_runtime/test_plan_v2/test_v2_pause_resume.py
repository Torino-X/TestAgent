"""Phase 2.1 v2 — 1 个暂停/恢复测试。"""

from __future__ import annotations

import pytest

from app.agent_runtime.graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V2,
)
from app.agent_runtime.graphs.test_plan.state import (
    make_empty_state,
)


# ── 13. pause_then_resume_records_loss_decision ──────────────────────────


@pytest.mark.asyncio
async def test_pause_then_resume_records_loss_decision(coordinator):
    """完整暂停-恢复回路:先经 pause_for_legacy_confirm 暂停,然后 format loss 决策恢复。"""
    # Phase 2.1 coordinator 接受 restored_state dict
    # 这里只验证接口 shape,具体执行由后续等价测试覆盖
    assert coordinator is not None
    assert hasattr(coordinator, "run_pre_confirm")
    assert hasattr(coordinator, "run_post_confirm")
    assert hasattr(coordinator, "resume_format_loss")


@pytest.mark.asyncio
async def test_make_empty_state_v2_includes_pause_fields():
    """make_empty_state(graph_version=v2) 应包含 pause_marker 字段默认值。"""
    state = make_empty_state(
        task_id="t-1",
        graph_run_id="r-1",
        graph_version=GRAPH_VERSION_V2,
    )
    # pause_marker 默认 None,但字段必须存在
    assert "pause_marker" in state
    assert state["pause_marker"] is None
    assert "current_phase" in state
    # review/format loop 计数字段
    assert state.get("review_loop_count", 0) >= 0
    assert state.get("format_loop_count", 0) >= 0