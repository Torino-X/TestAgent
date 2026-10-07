"""TestAgent production LangGraph runtime package.

This package owns the only executable Agent runtime and is responsible for:

* 定义 LangGraph 节点共享的状态协议 (``graphs.test_plan.state``)
* 编译可执行的 StateGraph (``graphs.test_plan.versions``)
* 暴露 ``GraphRegistry`` / ``GraphRuntimeService`` 给测试与 Phase 2.1+
* 通过 ``AgentEventSink`` / ``CancellationService`` 把运行时副作用与
  真实 SSE / EventPublisher 解耦 (Phase 2.0 仅 stub)

The runtime keeps graph execution separate from FastAPI observation endpoints;
database, LLM and tool access enter through explicit runtime adapters.
"""

__all__ = [
    "feature_flags",
    "runtime_context",
    "cancellation",
    "events",
    "persistence",
    "graphs",
    "graph_registry",
    "graph_runtime_service",
    "api_dispatcher",
]

# 子包职责(Phase 2.0+):
#   * 共享状态契约:graphs.test_plan.state.TestPlanGraphState (TypedDict)
#   * 编译图:graphs.test_plan.versions.{v1,v2,v2_frozen,v3} 的 build_xxx_graph
#   * LangGraph 入口适配:langgraph_dispatch_adapter + langgraph_run_coordinator
#   * Outbox worker:AgentExecutionWorker (被 app.main lifespan 挂)
#   * 图注册:graph_registry (按 feature flag / version 装入或屏蔽)
#   * 运行时上下文:runtime_context.RuntimeContext (frozen + slots)
#   * 错误:dispatch_errors (继承 RuntimeError,进程级)
#
# 阅读建议:
#   ┌─ 从 graphs/test_plan/versions/v3/graph.py 看节点拓扑
#   ├─ 从 langgraph_run_coordinator 看出图调用
#   ├─ 从 runtime_context 看节点 ↔ LLM / DB / SSE 的连接
#   ├─ 从 langgraph_dispatch_adapter 看 from Tool 适配到 LLMClient / EventSink
#   └─ 从 graph_registry 看版本切换逻辑
