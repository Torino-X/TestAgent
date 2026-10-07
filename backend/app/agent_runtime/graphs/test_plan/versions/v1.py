"""Test Plan Graph v1 - Phase 2.0 stub。

不调用 LLM / Tool / Session / EventPublisher。
仅满足 ``START → initialize_stub → END`` 的最小可工作图。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from ..constants import GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V1, STATE_SCHEMA_VERSION
from ..state import TestPlanGraphState

NODE_INITIALIZE_STUB = "initialize_stub"


def initialize_stub(state: TestPlanGraphState) -> dict:
    """节点: 初始化 state,标记 task 为 running。

    Phase 2.0 stub 不读写数据库,不发事件,不调任何 IO。
    """
    completed = list(state.get("completed_nodes") or [])
    if NODE_INITIALIZE_STUB not in completed:
        completed.append(NODE_INITIALIZE_STUB)
    return {
        "current_node": NODE_INITIALIZE_STUB,
        "current_phase": "initialized",
        "task_status": "running",
        "completed_nodes": completed,
    }


def build_test_plan_v1_graph(checkpointer=None):
    """编译 ``START → initialize_stub → END`` 最小图。

    :param checkpointer: ``MemorySaver`` 实例或 None。无 checkpointer 时
                         仍然能 ``ainvoke``,但 ``get_state`` 不可用。
    """
    graph = StateGraph(TestPlanGraphState)
    graph.add_node(NODE_INITIALIZE_STUB, initialize_stub)
    graph.add_edge(START, NODE_INITIALIZE_STUB)
    graph.add_edge(NODE_INITIALIZE_STUB, END)
    return graph.compile(
        checkpointer=checkpointer,
        name=f"{GRAPH_NAME_TEST_PLAN}_{GRAPH_VERSION_V1}",
    )


__all__ = [
    "NODE_INITIALIZE_STUB",
    "STATE_SCHEMA_VERSION",
    "build_test_plan_v1_graph",
    "initialize_stub",
]