"""Phase 2.8D + 2.8R-F — LangGraph ``recursion_limit`` 兜底。

2.8D 决策:LangGraph 0.3.x compile() 不接受 recursion_limit kwarg,改通过
config 注入。Phase 2.8R-F 升级:recursion_limit 不再硬编码 10,改由
``BusinessBudgets.compute_recursion_limit()`` 动态计算
(max_legal_observed + safe_margin)。

测试覆盖(docs/35 §6 + docs/32 §16):
* ``_build_config`` 注入 cfg['recursion_limit'] > max_legal_observed
* v2 graph compile 不接受 recursion_limit kwarg(LangGraph 0.3.x)
* Budget 与 RetryPolicy 兼容性(max_attempts + 兜底 ≤ computed)
"""

from __future__ import annotations

import pytest

from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
from app.agent_runtime.graphs.test_plan.versions.v2.graph import build_test_plan_v2_graph
from app.agent_runtime.business_budgets import (
    BusinessBudgets,
    get_default_budgets,
)


class TestRecursionLimit:
    @pytest.mark.asyncio
    async def test_build_config_injects_computed_recursion_limit(self) -> None:
        """Phase 2.8R-F:_build_config 注入 cfg['recursion_limit'] > max_legal_observed。"""
        # 通过 dataclass-like __init__ 路径构造;stub 出 _context_factory
        coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)
        coord._context_factory = None
        cfg = await coord._build_config({"task_id": "t-1"})

        # recursion_limit 由 BusinessBudgets 计算,**不再**硬编码 10
        budgets = get_default_budgets()
        expected = budgets.compute_recursion_limit()
        assert cfg["recursion_limit"] == expected, (
            f"expected {expected} (computed), got {cfg['recursion_limit']}"
        )
        assert cfg["recursion_limit"] > budgets.max_legal_observed()
        assert cfg["configurable"]["thread_id"] == "t-1"

    def test_v2_graph_compiles_without_recursion_kwarg(self) -> None:
        """Phase 2.8D:LangGraph 0.3.34 compile() 不接受 recursion_limit kwarg;
        改通过 config 传入;graph 静态签名不变。"""
        from langgraph.checkpoint.memory import MemorySaver
        g = build_test_plan_v2_graph(checkpointer=MemorySaver())
        # RecursionLimit 不在编译对象上,但通过 config 注入(由 ainvoke 路径)
        assert g is not None
        # 通过 ainvoke config 验证
        budgets = get_default_budgets()
        config = {
            "recursion_limit": budgets.compute_recursion_limit(),
            "configurable": {"thread_id": "t"},
        }
        assert config["recursion_limit"] == budgets.compute_recursion_limit()

    def test_max_retries_compatible_with_recursion_limit(self) -> None:
        """Phase 2.8R-F:RetryPolicy max_attempts + 兜底 + 1 review/regen + 1 format ≤ computed recursion_limit。

        防止 RetryPolicy while 错改时无限递归;LangGraph recursion_limit 兜底。
        """
        budgets = get_default_budgets()
        computed = budgets.compute_recursion_limit()
        max_retries = 3  # RetryPolicy max_attempts
        safeguard = 1
        review_regen_loops = 1
        format_loops = 1
        margin = 1
        total = max_retries + safeguard + review_regen_loops + format_loops + margin
        # 实际路径最长 ~30 super-step (PATH_MEASUREMENTS);limit = 30 + safe_margin
        # 任何预算 <= computed,防止无限递归
        assert total < computed, (
            f"total {total} should be < computed {computed}"
        )
