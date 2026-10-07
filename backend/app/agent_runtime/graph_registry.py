"""GraphRegistry —— graph_name + version → 编译过的 graph。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from .graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V1,
    GRAPH_VERSION_V2,
    GRAPH_VERSION_V3,
    state_schema_version_for,
)
from .graphs.test_plan.graph import compile_test_plan_graph


class GraphVersionNotFound(KeyError):
    """请求的 ``graph_name`` / ``version`` 未注册。"""


@dataclass
class GraphRegistry:
    """简单的进程内注册表。

    特性:
    * 显式 ``register`` / ``get`` API,无全局可变状态
    * 测试可 build 一个独立 registry (空 / 含特定版本) 完全隔离
    * ``build_default`` 工厂注册 v1 + 默认 checkpointer
    """

    name: str = "default"
    _entries: Dict[Tuple[str, str], Any] = field(default_factory=dict)
    _schemas: Dict[Tuple[str, str], int] = field(default_factory=dict)

    def register(self, name: str, version: str, compiled: Any, *, schema_version: int = 1) -> None:
        key = (name, version)
        if key in self._entries:
            raise ValueError(f"graph {key} already registered")
        self._entries[key] = compiled
        self._schemas[key] = schema_version

    def get(self, name: str, version: str) -> Any:
        key = (name, version)
        if key not in self._entries:
            raise GraphVersionNotFound(f"graph {key} not registered in registry {self.name!r}")
        return self._entries[key]

    def schema_version(self, name: str, version: str) -> int:
        key = (name, version)
        if key not in self._schemas:
            raise GraphVersionNotFound(f"graph {key} not registered in registry {self.name!r}")
        return self._schemas[key]

    def list_versions(self, name: str) -> List[str]:
        return [v for (n, v) in self._entries.keys() if n == name]

    @classmethod
    def build_default(cls, checkpointer: Any = None, *, include_v2: bool = False) -> "GraphRegistry":
        """Phase 2.1 改动:``include_v2=True`` 时同时注册 v1 + v2。

        生产热路径仍只注册 v1(默认);只有测试或 Phase 2.1 显式 opt-in
        才打开 v2 注册,避免生产 import 引发动静。
        """
        reg = cls(name="default")
        compiled_v1 = compile_test_plan_graph(version=GRAPH_VERSION_V1, checkpointer=checkpointer)
        reg.register(
            GRAPH_NAME_TEST_PLAN,
            GRAPH_VERSION_V1,
            compiled_v1,
            schema_version=state_schema_version_for(GRAPH_VERSION_V1),
        )
        if include_v2:
            compiled_v2 = compile_test_plan_graph(version=GRAPH_VERSION_V2, checkpointer=checkpointer)
            reg.register(
                GRAPH_NAME_TEST_PLAN,
                GRAPH_VERSION_V2,
                compiled_v2,
                schema_version=state_schema_version_for(GRAPH_VERSION_V2),
            )
        return reg

    @classmethod
    def build_default_v2(cls, checkpointer: Any = None) -> "GraphRegistry":
        """Phase 2.1 / 2.8R:同时注册 v1 + v2(v2 指向 v2_frozen)。

        内部测试 / Phase 2.1 显式调用。
        不挂生产热路径,仅供 registry 形式注册,不引发动态副作用。

        Phase 2.8R-D 更新:``v2`` 现在指向 ``v2_frozen`` 冻结实现;
        生产 v2 任务重放会走这条路径。
        """
        reg = cls(name="default_v2")
        compiled_v1 = compile_test_plan_graph(version=GRAPH_VERSION_V1, checkpointer=checkpointer)
        reg.register(
            GRAPH_NAME_TEST_PLAN,
            GRAPH_VERSION_V1,
            compiled_v1,
            schema_version=state_schema_version_for(GRAPH_VERSION_V1),
        )
        compiled_v2 = compile_test_plan_graph(version=GRAPH_VERSION_V2, checkpointer=checkpointer)
        reg.register(
            GRAPH_NAME_TEST_PLAN,
            GRAPH_VERSION_V2,
            compiled_v2,
            schema_version=state_schema_version_for(GRAPH_VERSION_V2),
        )
        return reg

    @classmethod
    def build_default_v2_v3(cls, checkpointer: Any = None) -> "GraphRegistry":
        """Phase 2.8R-D:同时注册 v1 + v2(v2_frozen) + v3。

        生产 lifespan 在 main.py 用本工厂,确保:
          * v1 → stub(向后兼容历史单测)
          * v2 → v2_frozen(历史 v2 任务 reload)
          * v3 → v3(生产新任务)

        v3 与 v2_frozen 是**独立目录、不可互换**:v3 builder 显式声明
        schema_version=V7、graph name=f"{name}_v3",registry 注册时
        v3 与 v2_frozen 指向不同的 compiled 对象(不可只赋值别名)。
        """
        reg = cls(name="default_v2_v3")
        compiled_v1 = compile_test_plan_graph(version=GRAPH_VERSION_V1, checkpointer=checkpointer)
        reg.register(
            GRAPH_NAME_TEST_PLAN,
            GRAPH_VERSION_V1,
            compiled_v1,
            schema_version=state_schema_version_for(GRAPH_VERSION_V1),
        )
        compiled_v2 = compile_test_plan_graph(version=GRAPH_VERSION_V2, checkpointer=checkpointer)
        reg.register(
            GRAPH_NAME_TEST_PLAN,
            GRAPH_VERSION_V2,
            compiled_v2,
            schema_version=state_schema_version_for(GRAPH_VERSION_V2),
        )
        compiled_v3 = compile_test_plan_graph(version=GRAPH_VERSION_V3, checkpointer=checkpointer)
        reg.register(
            GRAPH_NAME_TEST_PLAN,
            GRAPH_VERSION_V3,
            compiled_v3,
            schema_version=state_schema_version_for(GRAPH_VERSION_V3),
        )
        from .graphs.dynamic_agent import (
            GRAPH_NAME_DYNAMIC_AGENT,
            GRAPH_VERSION_DYNAMIC_AGENT_V1,
            GRAPH_VERSION_DYNAMIC_AGENT_V3,
            build_dynamic_agent_v1_graph,
        )
        from .graphs.dynamic_agent.constants import (
            STATE_SCHEMA_VERSION_DYNAMIC_AGENT_V1,
        )

        compiled_dynamic_agent = build_dynamic_agent_v1_graph(checkpointer=checkpointer)
        reg.register(
            GRAPH_NAME_DYNAMIC_AGENT,
            GRAPH_VERSION_DYNAMIC_AGENT_V1,
            compiled_dynamic_agent,
            schema_version=STATE_SCHEMA_VERSION_DYNAMIC_AGENT_V1,
        )
        reg.register(
            GRAPH_NAME_DYNAMIC_AGENT,
            GRAPH_VERSION_DYNAMIC_AGENT_V3,
            compiled_dynamic_agent,
            schema_version=STATE_SCHEMA_VERSION_DYNAMIC_AGENT_V1,
        )
        return reg

    @classmethod
    def build_default_v2_v3_with_pilot(cls, checkpointer: Any = None) -> "GraphRegistry":
        """CE-02 WP-10:默认 v1+v2+v3 + 受 Feature Flag 门控的 ce_pilot 注册。

        当 ``CONTEXT_ENGINE_ENABLED`` 开启时额外注册 ce_pilot 图；
        flag 关闭时与 ``build_default_v2_v3`` 行为完全一致（不改变 default graph）。
        """
        reg = cls.build_default_v2_v3(checkpointer=checkpointer)
        from app.context_engine.feature_flags import get_context_engine_flags

        flags = get_context_engine_flags()
        if flags.context_engine_enabled:
            from app.agent_runtime.graphs.ce_pilot import (
                GRAPH_NAME_PILOT,
                GRAPH_VERSION_PILOT,
                STATE_SCHEMA_VERSION_PILOT,
                build_compiled_ce_pilot_graph,
            )

            compiled_pilot = build_compiled_ce_pilot_graph(checkpointer=checkpointer)
            reg.register(
                GRAPH_NAME_PILOT,
                GRAPH_VERSION_PILOT,
                compiled_pilot,
                schema_version=STATE_SCHEMA_VERSION_PILOT,
            )
        return reg

    @classmethod
    def build_default_incremental_v1(
        cls, checkpointer: Any = None,
    ) -> "GraphRegistry":
        """Phase 2.5:注册 incremental_test_plan v1 subgraph。

        独立 graph_name + version,与 test_plan_generation 主图隔离;
        schema_version=5(STATE_SCHEMA_VERSION_V5)。

        Phase 2.5 范围内仅供内部测试显式调用,不挂生产热路径。
        """
        from .incremental.subgraph import (
            GRAPH_NAME_INCREMENTAL,
            GRAPH_VERSION_INCREMENTAL_V1,
            GRAPH_VERSION_INCREMENTAL_V3,
            build_incremental_subgraph,
        )
        from .graphs.test_plan.constants import STATE_SCHEMA_VERSION_V5

        reg = cls(name="default_incremental_v1")
        compiled_inc = build_incremental_subgraph(checkpointer=checkpointer)
        reg.register(
            GRAPH_NAME_INCREMENTAL,
            GRAPH_VERSION_INCREMENTAL_V1,
            compiled_inc,
            schema_version=STATE_SCHEMA_VERSION_V5,
        )
        reg.register(
            GRAPH_NAME_INCREMENTAL,
            GRAPH_VERSION_INCREMENTAL_V3,
            compiled_inc,
            schema_version=STATE_SCHEMA_VERSION_V5,
        )
        return reg


__all__ = ["GraphRegistry", "GraphVersionNotFound"]


# 模块定位:GraphRegistry — graph_name + version → 编译过的 graph
#
# 注册时显式声明 GRAPH_VERSION_V3;未知 version → GraphVersionNotAvailableError,
# 不 fallback(default graph 始终是 v3)。
#
# 数据形态:
#   { graph_name: { version_str: compiled_graph } }
#   启用 / 屏蔽通过 feature_flags.graph_version_active_flags。
#
# 链路:
#   startup → 遍历 _REGISTRY 注册 → dispatcher.ainvoke(graph_name, ...)
#     → graph_registry.get(name, version) → compiled_graph
#     → ainvoke(state, config)
#
# 关键约束:
#   - default graph 是 v3(由 default_state_schema_version_for('v3') == V7);
#   - v2 / v2_frozen 仅在显式切回场景下装载(Phase 2.7 灰度);
#   - 注册的 graph 必须是已经 compile(checkpointer=...) 的,
#     不要把 state_graph 半成品塞进 registry。
