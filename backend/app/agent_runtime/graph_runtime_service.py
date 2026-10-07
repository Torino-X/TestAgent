"""GraphRuntimeService - 编译过的图的统一执行入口。"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

from app.core.config import get_settings

from .graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V1,
    GRAPH_VERSION_V2,
    GRAPH_VERSION_V3,
)
from .graphs.test_plan.state import (
    RuntimeContextLeakedIntoState,
    TestPlanGraphState,
    assert_state_serializable,
)
from .graph_registry import GraphRegistry, GraphVersionNotFound
from .graph_thread_id import require_graph_thread_id


# Phase 2.8R-D: 生产默认 v3。State 缺 graph_version 时用 settings 默认,
# 不再静默回退 v1(v1 是 Phase 2.0 stub,生产不可走)。
_KNOWN_VERSIONS = (GRAPH_VERSION_V1, GRAPH_VERSION_V2, GRAPH_VERSION_V3)


def _resolve_thread_id(state: TestPlanGraphState) -> str:
    """从 state 严格推断 ``thread_id``。Phase 2.8R-C:task_id 缺失/空 → 抛错。

    任何调用方注入 ``{task_public_id: ...}`` 即可;``task_id``(int)不行。
    """
    return require_graph_thread_id(state, context="graph_runtime")


def _resolve_graph_version(state: TestPlanGraphState) -> str:
    """从 state / settings 决定 graph_version。

    优先级:
      1. ``state["graph_version"]``(任务落库的版本,Phase 2.8R-D 不可变)
      2. ``settings.agent_runtime_default_graph_version``(生产默认 v3)

    未知 / 未注册版本 → 抛 ``GraphVersionNotFound``(Registry 已抛该异常)。
    """
    version = state.get("graph_version")
    if version:
        return str(version)
    s = get_settings()
    default = getattr(s, "agent_runtime_default_graph_version", None) or GRAPH_VERSION_V3
    return str(default)


class GraphRuntimeService:
    """包装 ``CompiledGraph`` 的轻量级执行门面。

    不持有状态 (除 registry);每次 invoke 都是 stateless 调用。
    """

    def __init__(self, registry: GraphRegistry) -> None:
        self._registry = registry

    async def ainvoke(
        self,
        state: TestPlanGraphState,
        *,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """执行图,返回终态 state。"""
        assert_state_serializable(dict(state))
        graph_name = state.get("graph_name") or GRAPH_NAME_TEST_PLAN
        version = _resolve_graph_version(state)
        compiled = self._registry.get(graph_name, version)
        cfg = config or {"configurable": {"thread_id": _resolve_thread_id(state)}}
        return await compiled.ainvoke(dict(state), config=cfg)

    async def astream(
        self,
        state: TestPlanGraphState,
        *,
        config: Optional[Dict[str, Any]] = None,
        mode: str = "values",
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式产出 state 增量。Phase 2.0 默认 ``values`` 模式。"""
        assert_state_serializable(dict(state))
        graph_name = state.get("graph_name") or GRAPH_NAME_TEST_PLAN
        version = _resolve_graph_version(state)
        compiled = self._registry.get(graph_name, version)
        cfg = config or {"configurable": {"thread_id": _resolve_thread_id(state)}}
        async for chunk in compiled.astream(dict(state), config=cfg, stream_mode=mode):
            yield chunk

    async def get_state(
        self,
        *,
        graph_name: str,
        version: str,
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """异步读取 checkpointer 中的 state。无 checkpointer 或 thread 不存在 → 空 dict。

        Phase 2.9A.6:必须用 ``compiled.aget_state()`` 替代同步 ``get_state()``。
        """
        compiled = self._registry.get(graph_name, version)
        snapshot = await compiled.aget_state(config)
        if snapshot is None:
            return {}
        values = getattr(snapshot, "values", None)
        return dict(values) if values else {}

    def schema_version(self, graph_name: str, version: str) -> int:
        return self._registry.schema_version(graph_name, version)

    @classmethod
    def build_default(
        cls, *, checkpointer: Any = None, include_v2: bool = False
    ) -> "GraphRuntimeService":
        """用默认 registry 构造 service。

        ``include_v2=True`` 时 registry 同时注册 v1 + v2(Phase 2.1 测试 opt-in)。
        生产路径保持 ``include_v2=False``,不引入 v2 副作用。
        """
        return cls(
            registry=GraphRegistry.build_default(
                checkpointer=checkpointer, include_v2=include_v2
            )
        )


__all__ = ["GraphRuntimeService"]

# 模块定位:编译过图的统一执行入口
#
# 链路:
#   external caller → service.ainvoke(graph_name, state, thread_id)
#     → graph_registry 取编译过的 graph
#     → set thread_id / recursion_limit / config['configurable']['runtime_context']
#     → graph.ainvoke(state, config)
#     → 收 partial updates,按 signal 字段 forward 给 event_sink
#
# 关键约束:
#   - 不在本 service 写 checkpoint commit(由 LangGraph compiled 自带);
#   - 必须按 thread_id 复用 checkpointer 配置;
#   - 失败必须 raise 而不是静默(由调用方 → ApiDispatcher.ainvoke 决定退避策略);
#   - 与 langgraph_run_coordinator 是平行关系(后者负责生命周期与 SSE,
#     本 service 只管图执行)。
