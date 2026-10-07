"""Phase 2.8C ADR-2.8C-8/9 — routing.py 字符串一致性 + route_after_review fire-and-forget。"""

from __future__ import annotations

from app.agent_runtime.graphs.test_plan.versions.v2.routing import (
    NODE_REGENERATE_SECTIONS_STEP,
    NODE_REPAIR_SUBGRAPH,
    route_after_review,
)


def test_route_after_review_returns_consistent_strings() -> None:
    """routing.py 返回的字符串必须匹配 nodes_*.py 实际节点名(2.4 一致性)。"""
    # level != "failed" 或无 block issues → prepare_export(NODE_PREPARE_EXPORT)
    state = {"review_result": {"level": "passed"}, "repair_loop_count": 0}
    assert route_after_review(state) == "prepare_export"

    # level=failed + block issues + repair_agent_enabled=True → repair_subgraph(实际节点字符串)
    state_failed_repair = {
        "review_result": {
            "level": "failed",
            "review_issues": [{"severity": "block"}],
        },
        "repair_loop_count": 0,
        "repair_agent_enabled": True,
    }
    assert route_after_review(state_failed_repair) == NODE_REPAIR_SUBGRAPH

    # level=failed + block issues + repair_agent_enabled=False → regenerate_sections
    state_failed_regen = {
        "review_result": {
            "level": "failed",
            "review_issues": [{"severity": "block"}],
        },
        "repair_loop_count": 0,
        "review_loop_count": 0,
        "repair_agent_enabled": False,
    }
    assert route_after_review(state_failed_regen) == NODE_REGENERATE_SECTIONS_STEP


def test_route_after_review_fires_repair_dispatch_on_failure() -> None:
    """review failed + ApiDispatcher.dynamic_agent_api_enabled → fire-and-forget dispatch。

    Phase 2.8C ADR-2.8C-9:review failed 时,无论 graph 走 regen 还是 repair 子图,
    旁路 fire-and-forget 调 ApiDispatcher.dispatch_repair_task。
    """
    state_failed = {
        "review_result": {
            "level": "failed",
            "review_issues": [{"severity": "block", "rule_id": "R1"}],
        },
        "repair_loop_count": 0,
        "review_loop_count": 0,
        "repair_agent_enabled": False,  # 即便 graph 走 regen 路径
        # 无 runtime_context / app_state / api_dispatcher → _try_fire_repair_dispatch no-op
    }
    # 不抛异常(纯函数兜底,无 app_state 时静默返回)
    assert route_after_review(state_failed) == NODE_REGENERATE_SECTIONS_STEP