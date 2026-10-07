"""Phase 2.8A Step 13 — ``main.py`` lifespan 集成测试(probe → 注入 → ApiDispatcher 单例)。

设计目标:
* 验证 lifespan 启动期按 7 步构造顺序正确执行:
  1. ``probe_at_startup`` → ``ProbeReport``
  2. ``build_postgres_checkpointer`` 或 ``build_inmemory_checkpointer`` → ``cp``
  3. ``GraphRegistry.build_default_v2(checkpointer=cp)`` → ``registry``
  4. ``GraphRuntimeService(registry=...)`` → ``graph_runtime``
  5. ``LangGraphRunCoordinator(registry, runtime, checkpointer=cp)`` → ``coordinator``
  6. ``LangGraphDispatchAdapter(coordinator)`` → ``adapter``
  7. ``ApiDispatcher(orchestrator, adapter, probe_report)`` → ``api_dispatcher``
  + 全部挂到 ``app.state.{...}``
* 双闸门触发的 fallback 路径:Postgres 探测失败 → MemorySaver fallback
  + ``production_dispatch_forced_off=True`` + ApiDispatcher 永远回退 Legacy。
* 验证 lifespan **不能阻塞** 即使 Postgres / LangGraph chain 都崩了,
  ApiDispatcher 仍能挂(可能为 None),进程不 crash。

为什么这些测试是 Step 13 必需的:
* Step 13 是 Phase 2.8A 唯一进入 main.py lifespan 的改动点;如果 lifespan
  crash,生产进程无法启动,损失大于 Phase 2.1-2.7 任何步骤。
* docs/29 §7.3 要求 lifespan 必须保证 ApiDispatcher singleton 就绪,
  ``app.state.api_dispatcher`` 是后续 API 调用的唯一入口。

守禁令映射:
* 守禁令 #18 → 测试断言 Postgres 探测失败时 production_dispatch_forced_off=True,
  阻止 LangGraph 生产任务用纯 MemorySaver 跑。
* 守禁令 #21 → InFlightTaskRegistry 单例在 lifespan 内由 ApiDispatcher 持有,
  Step 13 测试只验证 ApiDispatcher 的 inflight property 暴露即可。
* 守禁令 #24 → 测试验证 Postgres 不可达时 ApiDispatcher._resolve_engine
  自动返回 (legacy, fallback_used=True),不抛错到 API。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import patch

import pytest

from app.agent_runtime.persistence import ProbeReport


def _build_minimal_probe_report(
    *,
    postgres_ok: bool = False,
    redis_ok: bool = True,
    eventbus_kind: str = "InMemory",
) -> ProbeReport:
    """构造一份最小可用的 ProbeReport(避免真探测)。"""
    return ProbeReport(
        postgres_ok=postgres_ok,
        postgres_url_echo="postgresql://user:***@localhost:5432/db" if postgres_ok else "",
        postgres_latency_ms=120 if postgres_ok else 0,
        eventbus_kind=eventbus_kind,
        eventbus_url_echo="redis://localhost:6379/0" if redis_ok else "",
        redis_ok=redis_ok,
        ready=redis_ok,
        production_dispatch_forced_off=not postgres_ok,
        warnings=() if postgres_ok else ("postgres probe failed",),
    )


# ──────────────────────────────────────────────────────────────────────────
# Path 1: lifespan 函数结构 — 验证 7 步构造顺序在源码中存在
# ──────────────────────────────────────────────────────────────────────────


def test_lifespan_function_is_async_context_manager() -> None:
    """main.py lifespan 必须是 ``@asynccontextmanager async def`` 形态。"""
    from app.main import lifespan

    # ``@asynccontextmanager`` 把函数变成可调用的 wrapper(在 Python 3.13 中
    # 仍表现为 ``function`` 类型),但 ``lifespan.__wrapped__`` 保留原 ``async def``
    # 函数 — 这里用它来验证 FastAPI 依赖的 async generator 契约。
    inner = getattr(lifespan, "__wrapped__", lifespan)
    # FastAPI lifespan 契约必须是 async generator function(因为内部 yield)
    assert inspect.isasyncgenfunction(inner), (
        "lifespan.__wrapped__ 必须是 async def + yield 的 async generator function"
    )
    # 调用 lifespan(app) 必须返回 async context manager 协议对象(有 __aenter__/__aexit__)。
    # 这是 @asynccontextmanager 的核心契约 — Python 3.13 没有 contextlib.isasynccontextmanager,
    # 我们用 Duck Type 检查。
    sentinel_app = type("_DummyApp", (), {})()
    ctx = lifespan(sentinel_app)
    assert hasattr(ctx, "__aenter__") and hasattr(ctx, "__aexit__"), (
        "lifespan(app) 必须返回 async context manager 协议对象 "
        "(即 @asynccontextmanager 包装后的 _AsyncGeneratorContextManager)"
    )
    assert type(ctx).__name__ == "_AsyncGeneratorContextManager", (
        f"lifespan(app) 应返回 _AsyncGeneratorContextManager;got {type(ctx).__name__}"
    )


def test_lifespan_references_seven_step_construction_chain() -> None:
    """lifespan 源码必须包含 7 步构造链的 7 个关键符号。

    按 Step 13 计划:
      probe_at_startup → build_postgres_checkpointer → build_inmemory_checkpointer
      → GraphRegistry.build_default_v2 → GraphRuntimeService
      → LangGraphRunCoordinator → LangGraphDispatchAdapter → ApiDispatcher
    """
    from app import main as main_module

    src = inspect.getsource(main_module.lifespan)

    required_symbols = [
        "probe_at_startup",
        "build_postgres_checkpointer",
        "build_inmemory_checkpointer",
        "GraphRegistry.build_default_v2",
        "GraphRuntimeService",
        "LangGraphRunCoordinator",
        "LangGraphDispatchAdapter",
        "ApiDispatcher",
        "aclose_postgres_checkpointer",
    ]
    for sym in required_symbols:
        assert sym in src, f"lifespan 源码缺少 Step 13 关键符号: {sym}"


def test_lifespan_mounts_all_required_state_keys() -> None:
    """lifespan 必须把所有 7 个对象挂到 ``app.state`` 上。"""
    from app import main as main_module

    src = inspect.getsource(main_module.lifespan)
    required_keys = [
        "app.state.probe_report",
        "app.state.checkpointer",
        "app.state.graph_runtime",
        "app.state.langgraph_coordinator",
        "app.state.langgraph_adapter",
        "app.state.api_dispatcher",
        "app.state.live_event_bus",
    ]
    for k in required_keys:
        assert k in src, f"lifespan 源码未挂 {k}"


def test_lifespan_postgres_checkpointer_aclose_on_shutdown() -> None:
    """shutdown 路径必须正确 aclose Postgres Checkpointer(避免连接池泄漏)。"""
    from app import main as main_module

    src = inspect.getsource(main_module.lifespan)
    assert "aclose_postgres_checkpointer" in src, (
        "shutdown 路径必须调 ``aclose_postgres_checkpointer`` 释放连接池"
    )
    # 在 yield 之后(下半段)
    yield_pos = src.find("yield")
    assert yield_pos > 0
    aclose_pos = src.find("aclose_postgres_checkpointer", yield_pos)
    assert aclose_pos > yield_pos, "aclose_postgres_checkpointer 必须在 yield 之后"


# ──────────────────────────────────────────────────────────────────────────
# Path 2: ApiDispatcher 真实构造 — 验证 7 步链路在隔离运行环境下可工作
# ──────────────────────────────────────────────────────────────────────────


def _build_dispatcher_for_probe(probe: ProbeReport) -> Any:
    """绕开 lifespan,直接调 ApiDispatcher 构造逻辑验证接口。

    与 lifespan 不同的是:这里不构造 v2 registry / coordinator(避免引入
    langgraph 状态图副作用);只验证 ``ApiDispatcher(...)`` 本身能在
    给定 ``ProbeReport`` 下成功挂 singleton + 暴露 inflight / probe_report 属性。
    """
    from app.agent_runtime.api_dispatcher import ApiDispatcher

    # 用 LangGraphDispatchAdapter 的真实实现,但 coordinator 传 None —
    # 真实 lifespan 里 adapter 一定非 None,但单元测试只需要验证 dispatcher
    # 构造本身不依赖具体 coordinator 类型。
    class _NoopCoordinator:
        async def run_pre_confirm(self, payload):  # pragma: no cover
            return None

        async def run_post_confirm(self, payload):  # pragma: no cover
            return None

        async def resume_section_confirmation(self, payload):  # pragma: no cover
            return None

        async def resume_format_loss_interrupt(self, payload):  # pragma: no cover
            return None

        async def run_incremental(self, payload):  # pragma: no cover
            return None

        async def run_repair(self, payload):  # pragma: no cover
            return None

    object.__setattr__(probe, "langgraph_readiness", probe.postgres_ok)
    return ApiDispatcher(
        coordinator=_NoopCoordinator(),
        probe_report=probe,
    )


def test_api_dispatcher_constructed_with_healthy_probe() -> None:
    """Postgres 健康 → ApiDispatcher 暴露 ``production_dispatch_forced_off=False``。"""
    probe = _build_minimal_probe_report(postgres_ok=True)
    dispatcher = _build_dispatcher_for_probe(probe)
    assert dispatcher.probe_report is probe
    assert dispatcher.probe_report.production_dispatch_forced_off is False
    assert len(dispatcher.inflight) == 0  # 进程启动期无 in-flight 任务


def test_api_dispatcher_constructed_with_unhealthy_probe() -> None:
    """Postgres 不可达 → ApiDispatcher 暴露 ``production_dispatch_forced_off=True``。

    这是守禁令 #18 的核心契约:**ProbeReport 强制关闭 LangGraph 生产路径**,
    不依赖 ApiDispatcher 自己判断。
    """
    probe = _build_minimal_probe_report(postgres_ok=False)
    dispatcher = _build_dispatcher_for_probe(probe)
    assert dispatcher.probe_report.production_dispatch_forced_off is True


def test_api_dispatcher_fails_closed_when_postgres_unhealthy() -> None:
    """Postgres 不可达时 LangGraph 任务必须明确失败且不得切换引擎。"""
    import dataclasses
    from app.core.exceptions import LangGraphNotReadyError
    probe = _build_minimal_probe_report(postgres_ok=False)
    dispatcher = _build_dispatcher_for_probe(probe)

    # ``AgentRuntimeFeatureFlags`` 是 frozen dataclass,不能 ``patch.object``;
    # 必须用 ``dataclasses.replace`` 创建新实例替换 dispatcher._flags。
    new_flags = dataclasses.replace(
        dispatcher.feature_flags,
        production_dispatch_enabled=True,
        langgraph_enabled=True,
    )
    dispatcher._flags = new_flags

    with pytest.raises(LangGraphNotReadyError):
        dispatcher._resolve_engine(
            task_public_id="t-fail-closed",
            task_engine_type="langgraph",
        )


def test_api_dispatcher_rejects_historical_legacy_task() -> None:
    """Historical Legacy task data is readable but no longer executable."""
    from app.core.exceptions import UnsupportedLegacyTaskError

    probe = _build_minimal_probe_report(postgres_ok=True)
    dispatcher = _build_dispatcher_for_probe(probe)
    with pytest.raises(UnsupportedLegacyTaskError):
        dispatcher._resolve_engine(
            task_public_id="t-legacy",
            task_engine_type="legacy",
        )


def test_api_dispatcher_inflight_registry_is_singleton() -> None:
    """``dispatcher.inflight`` 必须暴露进程级 InFlightTaskRegistry(守禁令 #21)。"""
    probe = _build_minimal_probe_report(postgres_ok=True)
    dispatcher = _build_dispatcher_for_probe(probe)
    inflight = dispatcher.inflight
    assert inflight is not None
    assert len(inflight) == 0
    # 单例性:同一 dispatcher 多次访问拿到同一对象
    assert dispatcher.inflight is inflight


# ──────────────────────────────────────────────────────────────────────────
# Path 3: LangGraphDispatchAdapter 真实接口契约
# ──────────────────────────────────────────────────────────────────────────


def test_langgraph_dispatch_adapter_satisfies_protocol() -> None:
    """LangGraphDispatchAdapter 必须满足 ``LangGraphCoordinatorProtocol``。"""
    from app.agent_runtime.api_dispatcher import LangGraphCoordinatorProtocol
    from app.agent_runtime.langgraph_dispatch_adapter import LangGraphDispatchAdapter

    class _FakeCoordinator:
        async def run_pre_confirm(self, **kwargs):
            return {"result": "pre_confirm"}

        async def run_post_confirm(self, **kwargs):
            return {"result": "post_confirm"}

        async def resume_section_confirmation(self, **kwargs):
            return {"result": "resume"}

    fake = _FakeCoordinator()
    adapter = LangGraphDispatchAdapter(fake)
    assert isinstance(adapter, LangGraphCoordinatorProtocol), (
        "LangGraphDispatchAdapter 必须满足 LangGraphCoordinatorProtocol;ApiDispatcher "
        "构造时显式 isinstance 校验(开发期 fail-fast)"
    )


@pytest.mark.asyncio
async def test_langgraph_dispatch_adapter_run_pre_confirm_invokes_coordinator() -> None:
    """Adapter.run_pre_confirm 必须正确把 ``payload`` 翻译成 ``(task_id, graph_run_id, initial_payload)``。

    这是 Step 13 lifespan 链路里的"5 → 6 → 7"步接口契约:Adapter 必须把
    ApiDispatcher 传入的 ``payload: Any`` 拆解成真实 coordinator 期望的 kwargs。
    """
    from dataclasses import dataclass
    from app.agent_runtime.langgraph_dispatch_adapter import LangGraphDispatchAdapter

    @dataclass
    class _Ctx:
        task_id: str = "t-adapter-001"
        status: str = "pending"

    captured: dict = {}

    class _SpyCoordinator:
        async def run_pre_confirm(self, *, task_id, graph_run_id, initial_payload):
            captured["task_id"] = task_id
            captured["graph_run_id"] = graph_run_id
            captured["initial_payload"] = initial_payload
            return {"status": "ok"}

    adapter = LangGraphDispatchAdapter(_SpyCoordinator())
    result = await adapter.run_pre_confirm(_Ctx())
    assert result == {"status": "ok"}
    assert captured["task_id"] == "t-adapter-001"
    assert captured["graph_run_id"].startswith("run-t-adapter-001-")
    # dataclass 路径走 dataclasses.asdict,字段全序列化
    assert captured["initial_payload"]["task_id"] == "t-adapter-001"
    assert captured["initial_payload"]["status"] == "pending"


# ──────────────────────────────────────────────────────────────────────────
# Path 4: lifespan 异常容错 — 任何一步失败都不应阻塞启动
# ──────────────────────────────────────────────────────────────────────────


def test_lifespan_has_exception_guards_on_each_step() -> None:
    """lifespan 每一步都有 try/except 兜底,失败不应阻塞启动。

    Step 13 的"严格不阻塞"契约:即使 probe_at_startup / build_postgres_checkpointer /
    coordinator 构造 / ApiDispatcher 构造全部失败,FastAPI 仍能起来并挂
    ``app.state.api_dispatcher = None``,由 agent_tasks.py fallback 走 legacy。
    """
    from app import main as main_module

    src = inspect.getsource(main_module.lifespan)
    # 关键步骤的 try 块至少出现 1 次 — 跳过 docstring 区域(找步骤名**实际调用**位置)
    # 简单做法:从第一个 "try:" 开始往后看,只数 try 块内的步骤出现
    required_steps = [
        ("probe_at_startup", "步骤 1"),
        ("build_postgres_checkpointer", "步骤 2"),
        ("LangGraphRunCoordinator", "步骤 4"),
        ("ApiDispatcher", "步骤 6"),
    ]
    # 步骤 3+4+5(registry_v2 → coordinator → adapter)在同一 try 块里,
    # 最远的是 LangGraphRunCoordinator → except 约 1000 字符;窗口设 1500 兜底。
    # 2.8C:ApiDispatcher 步骤(env 透传 + replace + kwargs)较长,扩到 2200。
    for sym, label in required_steps:
        # 找 sym 的所有出现位置;取第一个不在 # 开头的位置(注释里)
        idx = -1
        search_start = 0
        while True:
            i = src.find(sym, search_start)
            if i < 0:
                break
            # 看该行是不是注释:向前找最近的 \n
            line_start = src.rfind("\n", 0, i) + 1
            line_prefix = src[line_start: i]
            if not line_prefix.lstrip().startswith("#"):
                idx = i
                break
            search_start = i + len(sym)
        assert idx > 0, f"lifespan 没找到 {label}({sym}) 的实际调用"
        # 取调用之后 2200 字符(覆盖整个 try 块 + except 兜底)
        snippet = src[idx: idx + 2200]
        assert "except" in snippet, (
            f"lifespan {label}({sym}) 调用之后 2200 字符内必须有 except 兜底"
        )


def test_lifespan_has_no_stub_coordinator_or_legacy_mount() -> None:
    """Startup mounts a fail-closed dispatcher without a fake coordinator."""
    from app import main as main_module

    assert not hasattr(main_module, "_StubCoordinator")
    src = inspect.getsource(main_module.lifespan)
    assert "legacy_orchestrator_mounted=false" in src
    assert "coordinator=adapter" in src


__all__ = [
    "_build_minimal_probe_report",
    "_build_dispatcher_for_probe",
]
