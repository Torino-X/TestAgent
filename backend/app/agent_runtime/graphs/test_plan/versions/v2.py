"""Test Plan Graph v2 - Phase 2.1 业务主图 facade。

按 ADR-2.1-3 单编译图 + 入口条件路由;节点集合在 ``v2.graph``,
side-effect helper(event sink + cancel buffer + tool adapter)在
``app.agent_runtime.adapters`` 和 ``app.agent_runtime.events``。
"""

from __future__ import annotations

from typing import Any, Optional

from ..constants import GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V2


def build_test_plan_v2_graph(checkpointer: Optional[Any] = None):
    """Phase 2.1 真实业务图 builder。

    :param checkpointer: ``MemorySaver`` 实例或 None。None 时 ``ainvoke``
                         仍能跑,但 ``get_state`` 不可用。
    """
    from .v2.graph import build_compiled_v2_graph

    compiled = build_compiled_v2_graph(checkpointer=checkpointer)
    # Keep imports referenced (for static analyzers / doc loaders)
    _ = (GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V2)
    return compiled


__all__ = ["build_test_plan_v2_graph"]
