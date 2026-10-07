"""Dynamic Agent graph constants."""

from __future__ import annotations

GRAPH_NAME_DYNAMIC_AGENT = "dynamic_agent"
GRAPH_VERSION_DYNAMIC_AGENT_V1 = "v1"
GRAPH_VERSION_DYNAMIC_AGENT_V3 = "v3"
STATE_SCHEMA_VERSION_DYNAMIC_AGENT_V1 = 1

__all__ = [
    "GRAPH_NAME_DYNAMIC_AGENT",
    "GRAPH_VERSION_DYNAMIC_AGENT_V1",
    "GRAPH_VERSION_DYNAMIC_AGENT_V3",
    "STATE_SCHEMA_VERSION_DYNAMIC_AGENT_V1",
]


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (Dynamic Agent 节点名称常量):
#
#   定义动态 Agent 图装配 graph.py 使用的所有节点名 / 路由键字符串常量。
#   集中维护便于 dynamic_agent 重构时(Phase 2.8C 阶段陆续加 Planner/Executor/
#   Verifier)避免散落的 magic string。
#
#   注意:与 test_plan/v3 的常量完全独立两套(测试计划是外部契约约束,
#         这里是对内可灵活演进)。
