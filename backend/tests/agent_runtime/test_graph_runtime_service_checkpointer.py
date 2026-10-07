"""Phase 2.8A Step 12 — ``GraphRuntimeService.build_default(checkpointer=...)`` 持久化 Checkpointer 接入。

设计目标:
* 证明 ``checkpointer`` 参数从 ``GraphRuntimeService.build_default`` 一路透传到
  编译好的 ``StateGraph.compile(checkpointer=...)`` 调用。
* 三条路径都需要验证:
  - ``GraphRuntimeService.build_default``(生产主路径,默认只注册 v1)
  - ``GraphRegistry.build_default_v2``(显式注册 v1 + v2)
  - ``GraphRegistry.build_default_incremental_v1``(Phase 2.5 独立子图)
* ``checkpointer=None`` → 编译图 ``.checkpointer`` 属性也为 None(no checkpointer)。
* ``checkpointer=MemorySaver()`` → 编译图 ``.checkpointer`` 是同一实例(identity)。
* 自定义 sentinel checkpointer(模拟 Postgres saver duck type)→ 同样透传。

为什么这些测试是 Step 12 必需的:
* docs/29 §7.2 要求 ``graph_runtime_service.build_default(checkpointer=...)`` 允许外部注入。
* Step 13 main.py lifespan 会从 ``probe_postgres_checkpointer()`` 拿到 ``AsyncPostgresSaver`` 实例,
  注入到 ``build_default(checkpointer=cp)``;如果这里某条路径丢了 cp,生产任务将无声无息退化到
  MemorySaver — 守禁令 #18(LangGraph 不能用纯 MemorySaver 在生产跑)会被默默违反。
* 故必须用 identity 断言确保透传不被任何中间层吞掉。

守禁令映射:
* 守禁令 #18 → 强制透传到 LangGraph compiled graph,生产注入真实 Postgres Saver。
* 守禁令 #21 → 测试只读 registry,不修改全局状态。
"""

from __future__ import annotations

import pytest

from app.agent_runtime.graph_registry import GraphRegistry
from app.agent_runtime.graph_runtime_service import GraphRuntimeService
from app.agent_runtime.graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V1,
    GRAPH_VERSION_V2,
)


class _SentinelCheckpointer:
    """Duck-typed checkpointer 占位 — 用于验证透传到编译图的 ``.checkpointer`` 属性。

    LangGraph ``CompiledStateGraph`` 在 ``.compile(checkpointer=...)`` 后会把对象挂到
    ``self.checkpointer``;类型不限,只要在 ``ainvoke`` / ``get_state`` 时能用即可。
    本测试不调用这些方法,只验证 identity 透传,所以 duck type 足够。
    """

    def __init__(self, tag: str = "sentinel") -> None:
        self.tag = tag


# ──────────────────────────────────────────────────────────────────────────
# Path 1: GraphRuntimeService.build_default (生产主路径)
# ──────────────────────────────────────────────────────────────────────────


def test_build_default_propagates_checkpointer_to_v1_only_registry() -> None:
    """``GraphRuntimeService.build_default(checkpointer=cp)`` 默认只注册 v1;v1 必须拿到 cp。"""
    cp = _SentinelCheckpointer(tag="runtime-v1")
    service = GraphRuntimeService.build_default(checkpointer=cp)
    compiled_v1 = service._registry.get(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V1)
    assert compiled_v1.checkpointer is cp, (
        "GraphRuntimeService.build_default 没把 checkpointer 透传到 v1 compiled graph"
    )


def test_build_default_checkpointer_none_propagates_none() -> None:
    """``checkpointer=None`` → 编译图 ``.checkpointer`` 也是 None(走"无 checkpointer"路径)。"""
    service = GraphRuntimeService.build_default(checkpointer=None)
    compiled_v1 = service._registry.get(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V1)
    assert compiled_v1.checkpointer is None, (
        "checkpointer=None 应让 compiled_v1.checkpointer 也为 None,而不是 fallback 到 MemorySaver"
    )


def test_build_default_default_checkpointer_is_none() -> None:
    """不带参数调 ``build_default()`` → ``checkpointer=None`` 透传到 v1(无 checkpointer)。

    Phase 2.8A Step 12 的契约:**不**默认给 MemorySaver — 默认 None 让 caller
    显式决定(Step 13 main.py lifespan 总是显式传 Postgres 或 MemorySaver)。
    早期 Phase 2.0 的 ``test_minimal_graph_thread_restore`` 显式构造 MemorySaver
    注入 — 测试环境才需要 in-memory 持久化,生产默认是 None(由 Step 13 lifespan 注入)。
    """
    service = GraphRuntimeService.build_default()
    compiled_v1 = service._registry.get(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V1)
    assert compiled_v1.checkpointer is None, (
        "build_default() 默认 checkpointer 应为 None;MemorySaver 必须显式注入"
    )


# ──────────────────────────────────────────────────────────────────────────
# Path 2: GraphRegistry.build_default_v2 (v1 + v2 都注册)
# ──────────────────────────────────────────────────────────────────────────


def test_registry_build_default_v2_propagates_to_both_versions() -> None:
    """``GraphRegistry.build_default_v2(checkpointer=cp)`` → v1 和 v2 都拿到 cp。"""
    cp = _SentinelCheckpointer(tag="registry-v2")
    reg = GraphRegistry.build_default_v2(checkpointer=cp)
    compiled_v1 = reg.get(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V1)
    compiled_v2 = reg.get(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V2)
    assert compiled_v1.checkpointer is cp, "v1 没拿到 checkpointer"
    assert compiled_v2.checkpointer is cp, "v2 没拿到 checkpointer"


def test_registry_build_default_v2_no_v2_register_when_checkpointer_none() -> None:
    """``checkpointer=None`` 透传到 v1 + v2;两个图都 ``.checkpointer is None``。"""
    reg = GraphRegistry.build_default_v2(checkpointer=None)
    v1 = reg.get(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V1)
    v2 = reg.get(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V2)
    assert v1.checkpointer is None
    assert v2.checkpointer is None


def test_registry_build_default_v2_independent_of_runtime_service() -> None:
    """直接调 ``GraphRegistry.build_default_v2``(绕过 ``GraphRuntimeService``)也能拿到 cp。

    Step 13 之前,测试代码可能直接用 ``GraphRegistry.build_default_v2`` 构造 — 必须保持兼容。
    """
    cp = _SentinelCheckpointer(tag="direct-registry")
    reg = GraphRegistry.build_default_v2(checkpointer=cp)
    # 两个图都拿到同一 cp
    v1 = reg.get(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V1)
    v2 = reg.get(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V2)
    assert v1.checkpointer is cp and v2.checkpointer is cp


# ──────────────────────────────────────────────────────────────────────────
# Path 3: GraphRegistry.build_default_incremental_v1 (Phase 2.5 独立子图)
# ──────────────────────────────────────────────────────────────────────────


def test_registry_build_default_incremental_v1_propagates_checkpointer() -> None:
    """Phase 2.5 增量子图也接受 checkpointer 注入(独立 graph_name)。"""
    from app.agent_runtime.graphs.test_plan.constants import STATE_SCHEMA_VERSION_V5
    from app.agent_runtime.incremental.subgraph import (
        GRAPH_NAME_INCREMENTAL,
        GRAPH_VERSION_INCREMENTAL_V1,
        GRAPH_VERSION_INCREMENTAL_V3,
    )

    cp = _SentinelCheckpointer(tag="incremental")
    reg = GraphRegistry.build_default_incremental_v1(checkpointer=cp)
    compiled_inc = reg.get(GRAPH_NAME_INCREMENTAL, GRAPH_VERSION_INCREMENTAL_V1)
    assert compiled_inc.checkpointer is cp, "增量子图没拿到 checkpointer"
    # schema_version 也得是 V5(防御性验证,确认是 incremental v1)
    assert reg.schema_version(GRAPH_NAME_INCREMENTAL, GRAPH_VERSION_INCREMENTAL_V1) == STATE_SCHEMA_VERSION_V5


def test_registry_build_default_incremental_v1_checkpointer_none() -> None:
    """增量子图 ``checkpointer=None`` 路径。"""
    from app.agent_runtime.incremental.subgraph import (
        GRAPH_NAME_INCREMENTAL,
        GRAPH_VERSION_INCREMENTAL_V1,
        GRAPH_VERSION_INCREMENTAL_V3,
    )

    reg = GraphRegistry.build_default_incremental_v1(checkpointer=None)
    compiled_inc = reg.get(GRAPH_NAME_INCREMENTAL, GRAPH_VERSION_INCREMENTAL_V1)
    assert compiled_inc.checkpointer is None
    assert reg.get(GRAPH_NAME_INCREMENTAL, GRAPH_VERSION_INCREMENTAL_V3) is compiled_inc


# ──────────────────────────────────────────────────────────────────────────
# 端到端行为测试 — MemorySaver 真线程恢复
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_saver_thread_restore_via_build_default() -> None:
    """完整链路:build_default(checkpointer=MemorySaver) → ainvoke → get_state 看到状态。

    镜像 Phase 2.0 已有的 test_minimal_graph_thread_restore,但**显式**走
    ``GraphRuntimeService.build_default(checkpointer=cp)`` 而不是直接构造 service —
    保证 build_default 的 checkpointer 参数走通整个生产热路径。
    """
    from app.agent_runtime.graphs.test_plan.state import make_empty_state
    from app.agent_runtime.persistence.checkpointer_factory import (
        build_inmemory_checkpointer,
    )

    cp = build_inmemory_checkpointer()
    service = GraphRuntimeService.build_default(checkpointer=cp)
    state = make_empty_state(task_id="step12-thread", graph_run_id="r-step12")
    config = {"configurable": {"thread_id": "step12-thread"}}

    result = await service.ainvoke(state, config=config)
    assert result["current_node"] == "initialize_stub"

    snapshot = await service.get_state(
        graph_name=GRAPH_NAME_TEST_PLAN,
        version=GRAPH_VERSION_V1,
        config=config,
    )
    assert snapshot.get("current_node") == "initialize_stub", (
        "MemorySaver 没把 invoke 后的 state 存下来 — checkpointer 没真正接入"
    )


# ──────────────────────────────────────────────────────────────────────────
# Step 13 集成预演 — 用 duck-typed Postgres-style saver 走 build_default
# ──────────────────────────────────────────────────────────────────────────


class _FakePostgresSaver:
    """模拟 AsyncPostgresSaver duck type。

    Step 13 main.py lifespan 会调 ``build_postgres_checkpointer(url)`` 拿到
    ``AsyncPostgresSaver`` 实例,然后传给 ``GraphRuntimeService.build_default(checkpointer=cp)``。
    本测试不连真 Postgres,只是验证「拿到 saver 后能塞进 build_default」这个**接口形状**正确。
    """

    def __init__(self) -> None:
        self.connects_to: list[str] = []

    async def setup(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def test_fake_postgres_saver_can_be_injected_into_build_default() -> None:
    """Step 13 接口预演:Postgres-style saver 能塞进 build_default。

    这步关键:**不要**在 build_default 里强制类型检查(不允许 isinstance Postgres Saver);
    LangGraph 自身对 checkpointer 类型无要求,只要求 duck type 满足 BaseCheckpointSaver 协议。
    """
    cp = _FakePostgresSaver()
    service = GraphRuntimeService.build_default(checkpointer=cp)
    compiled_v1 = service._registry.get(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V1)
    assert compiled_v1.checkpointer is cp
    # 类型任意即可,build_default 不强制
    assert isinstance(compiled_v1.checkpointer, _FakePostgresSaver)


def test_fake_postgres_saver_can_be_injected_into_registry_v2() -> None:
    """v2 路径同样支持 Postgres-style saver。"""
    cp = _FakePostgresSaver()
    reg = GraphRegistry.build_default_v2(checkpointer=cp)
    v1 = reg.get(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V1)
    v2 = reg.get(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V2)
    assert v1.checkpointer is cp
    assert v2.checkpointer is cp


__all__ = [
    "_SentinelCheckpointer",
    "_FakePostgresSaver",
]
