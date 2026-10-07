"""图版本选择 + 编译入口 — Phase 2.8R-D 含 v3。

设计要点(对应 docs/35 §4 验收五):
  * ``v1`` / ``v2_frozen`` / ``v3`` 都独立注册。
  * 未知 ``version`` → ``GraphVersionNotAvailableError``,**不**回退 latest
    (守 #18)。Phase 2.8R-A 之前历史代码用 ``ValueError``,现改为异常类。
  * ``version="v2"`` 在 Phase 2.8R-D 之后指向 ``v2_frozen`` 实现
    (历史 v2 实现迁到 ``v2_frozen`` 后,生产 v2 必须冻结)。
  * 默认 ``v3`` 当 caller 未传 version 时(Phase 2.8R-D 新增默认)。
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import GraphVersionNotAvailableError

from .constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V1,
    GRAPH_VERSION_V2,
    GRAPH_VERSION_V3,
)
from .versions.v1 import build_test_plan_v1_graph


# Phase 2.8R-D 修复后, ``v2`` 默认从 v2_frozen 加载(冻结版)。
# 未知 / 已弃用 / 未注册的 version 抛 GraphVersionNotAvailableError,不静默回退。
_DEFAULT_GRAPH_VERSION = GRAPH_VERSION_V3


def compile_test_plan_graph(
    version: str | None = None,
    *,
    checkpointer: Any = None,
):
    """根据 ``version`` 选择 builder。

    支持:
      * ``v1``(Phase 2.0 stub)
      * ``v2``(Phase 2.8R-D 起指向 ``v2_frozen``,历史 v2 实现已冻结)
      * ``v3``(Phase 2.8R-D 独立版本,生产默认)

    Args:
        version: 图版本标识,默认 ``v3``(Phase 2.8R-D 新默认)。
        checkpointer: LangGraph checkpointer(MemorySaver / PostgresSaver)。

    Returns:
        编译后的 StateGraph。

    Raises:
        GraphVersionNotAvailableError: 未知 version(**不**回退 latest)。
    """
    version = version or _DEFAULT_GRAPH_VERSION
    if version == GRAPH_VERSION_V1:
        return build_test_plan_v1_graph(checkpointer=checkpointer)
    if version == GRAPH_VERSION_V2:
        # Phase 2.8R-D: ``v2`` 显式加载 v2_frozen(冻结实现)。
        from .versions.v2_frozen import build_test_plan_v2_graph as build_test_plan_v2_frozen_graph

        return build_test_plan_v2_frozen_graph(checkpointer=checkpointer)
    if version == GRAPH_VERSION_V3:
        from .versions.v3 import build_test_plan_v3_graph

        # Phase 2.9A.7: v3 默认 ``interrupt_enabled=True``,确保真实
        # ``section_confirmation_interrupt`` 节点被注册,而不是 sentinel
        # ``pause_marker`` → END 路径。后者导致 Resume 时 LangGraph
        # 找不到挂起点而重跑整个 graph,最终 task 仍停在
        # ``waiting_user_confirm`` — Worker 记录 dispatched 但
        # TestPlanGeneratorTool 永远不开始(静默 no-op)。
        return build_test_plan_v3_graph(
            checkpointer=checkpointer,
            interrupt_enabled=True,
        )

    raise GraphVersionNotAvailableError(
        graph_name=GRAPH_NAME_TEST_PLAN,
        graph_version=version or "<empty>",
        detail={
            "graph_version": version or "<empty>",
            "known_versions": [GRAPH_VERSION_V1, GRAPH_VERSION_V2, GRAPH_VERSION_V3],
        },
    )


__all__ = ["compile_test_plan_graph"]
