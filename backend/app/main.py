"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import sys

# ── Windows Selector Event Loop Policy (must run BEFORE any asyncio call) ──
# psycopg (langgraph.checkpoint.postgres async driver) refuses to run on
# Windows' default ProactorEventLoop.  Switch to SelectorEventLoop at the
# very top so all subsequent asyncio code (uvicorn, lifespan, probes) uses
# a compatible loop.  Linux/macOS already default to Selector → no-op.
# scripts/run_dev.py has the same fix; this guards the case where someone
# runs ``uvicorn app.main:app`` directly without the helper script.
#
# Must also be reflected in the early startup banner so that operators can
# confirm the policy actually took effect (uvicorn --reload spawns a separate
# server subprocess; if the fix only ran in the reloader process, the server
# child would still see Proactor).
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        # Make the choice visible in stdout — reloader / server subprocesses
        # both print this on cold start, so the operator can spot a mismatch.
        print(
            f"[main] event loop policy = {asyncio.get_event_loop_policy().__class__.__name__}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[main] WARN: failed to switch event loop policy: {exc!r}", flush=True)

# ── Load .env into os.environ BEFORE any other imports ──────────
# ``override=False`` means values already set in the process environment
# (e.g. via systemd / docker / k8s) win over the values in ``.env``.
# This is critical for production: a stale ``.env`` must not silently
# overwrite an API key that ops has rotated.
# pydantic-settings also reads ``.env`` directly when constructing
# ``Settings``, so application config sees both sources consistently.
# The legacy development note below predates the unified resolver and is
# intentionally superseded: explicit process values must win in development
# as well as production.
# Phase 2.8R-K dev 友好:override=True 让 .env 总是覆盖系统 env(避免系统残留
# env var 阻止 .env 修改生效)。生产部署应改回 override=False(系统 env 优先)。
from dotenv import load_dotenv
# Keep process-environment values authoritative.  Context Engine's provider
# resolver uses the same precedence, so direct factories and FastAPI startup
# cannot disagree about credentials or migration flags.
load_dotenv(override=False)

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.core.config import get_settings  # noqa: E402

# Phase 2.8R-K 第十一处:.env 可能在模块 import 时(在 load_dotenv 之前)
# 已被 pydantic-settings 读过一次(且 Settings 内部 LRU 缓存了旧 .env 值),
# override=True 写 os.environ 不影响 cached Settings 对象。
# 重启时再 load_dotenv 之后清缓存,让 pydantic-settings 在 lifespan 里重读 .env。
get_settings.cache_clear()
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import router as v1_router
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import LogEvent, RequestLoggingMiddleware, log_event, setup_logging
from app.core.response import error as error_response
from app.tools.register import register_all_tools

settings = get_settings()
logger = logging.getLogger(__name__)


# Module-level logging setup — must run BEFORE uvicorn installs its
# own ``LOGGING_CONFIG`` (which happens during ``Server.startup``
# before the lifespan callback fires).  Calling this inside lifespan
# leaves the root logger with only uvicorn's handler, swallowing every
# business ``logger.info()`` / ``logger.warning()`` call.
setup_logging()


def _http_status_for_app_error(code: int) -> int:
    """Map an AppError code to an HTTP status code."""
    if 40001 <= code <= 40099:
        return 400
    if 40101 <= code <= 40199:
        return 401
    if 40301 <= code <= 40399:
        return 403
    if 40401 <= code <= 40499:
        return 404
    if 40901 <= code <= 40999:
        return 409
    if 41301 <= code <= 41399:
        return 413
    if 42201 <= code <= 42299:
        return 422
    # 509xx — Phase 2.9A.26 message feedback / regeneration domain.  Every
    # error in this range is a *business* validation/conflict (not a server
    # fault): 50901 non-feedbackable target, 50911 not the latest reply,
    # 50912 already running, 50913 context missing.  Treating them as 500
    # trips server-failure alerting and hides the fact that the caller's
    # request cannot be satisfied against the current conversation state.
    if 50901 <= code <= 50999:
        return 409
    if 50001 <= code <= 50099:
        return 500
    if 50101 <= code <= 50199:
        return 501
    if 50401 <= code <= 50499:
        return 503
    return 500


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup & shutdown hooks."""
    log_event(logger, logging.INFO, LogEvent.APPLICATION_STARTUP_STARTED, "Application startup started")
    # A release that explicitly opts into full-chain Context Engine operation
    # must never begin serving requests with one of the Agent LLM migrations
    # absent.  Failing startup is safer than silently producing legacy tasks.
    from app.context_engine.feature_flags import validate_full_chain_configuration

    validate_full_chain_configuration()
    register_all_tools()

    # 2026-07-14：把 OCR worker 子进程 spawn 放到 lifespan 阶段。
    # 这会触发 ``OcrProcessClient.ensure_ready``：Popen + 等 ``ready``（首次会
    # 下载模型权重，约 5-10s）。在事件循环外（``asyncio.to_thread``）做，
    # 否则会卡住 FastAPI 的 event loop、``/docs`` 健康检查等都会超时。
    try:
        from app.common.ocr_service import ocr_service
        if ocr_service.available:
            import asyncio
            await asyncio.to_thread(ocr_service._ensure_ocr)
            logger.info("OCR worker 启动预热完成（profile=%s）", ocr_service._profile)
    except Exception as exc:  # noqa: BLE001 — 预热失败不应阻塞启动
        logger.warning("OCR worker 启动预热失败，按需懒加载兜底 | err=%s", exc)

    # ════════════════════════════════════════════════════════════════════
    # Phase 2.8A Step 13 + Phase 2.8B: lifespan 集成
    #   probe_at_startup → ProbeReport → Postgres checkpointer (or MemorySaver)
    #   → verify_postgres_4_tables → GraphRuntimeService(include_v2=True, checkpointer=cp)
    #   → LangGraphRunCoordinator(registry, runtime, checkpointer=cp)
    #   → LangGraphDispatchAdapter(coordinator)
    #   → RedisInFlightRegistry (Phase 2.8B 跨 worker 锁)
    #   → ApiDispatcher(coordinator=adapter, probe_report=probe, redis_inflight=...)
    #   → app.state.{api_dispatcher, graph_runtime, langgraph_coordinator,
    #                langgraph_adapter, probe_report, live_event_bus, checkpointer,
    #                redis_inflight}
    # ════════════════════════════════════════════════════════════════════
    # 1. 探测(Postgres + LiveEventBus + Redis InFlight;timeout 8s)
    redis_inflight = None
    from app.agent_runtime.persistence import (  # noqa: E402
        build_postgres_checkpointer,
        build_inmemory_checkpointer,
        aclose_postgres_checkpointer,
        RedisInFlightRegistry,
        verify_postgres_4_tables,
    )
    try:
        from app.agent_runtime.persistence import probe_at_startup as _run_probe  # noqa: E402
        probe = await _run_probe(
            postgres_url=settings.agent_runtime_postgres_url,
            redis_url=settings.agent_runtime_redis_url,
            redis_inflight_url=settings.agent_runtime_redis_inflight_url,
            redis_inflight_ttl_seconds=settings.agent_runtime_redis_inflight_ttl_seconds,
        )
        app.state.probe_report = probe
        logger.info(
            "Phase 2.8A+2.8B ProbeReport | pg_ok=%s pg_latency=%dms | eventbus=%s redis_ok=%s "
            "| in_flight_backend=%s in_flight_ttl=%ds redis_inflight_ok=%s "
            "| ready=%s prod_forced_off=%s",
            probe.postgres_ok,
            probe.postgres_latency_ms,
            probe.eventbus_kind,
            probe.redis_ok,
            probe.in_flight_lock_backend,
            probe.in_flight_lock_ttl_seconds,
            probe.redis_inflight_ok,
            probe.ready,
            probe.production_dispatch_forced_off,
        )
    except Exception as exc:  # noqa: BLE001 — 探测失败不应该阻塞启动
        logger.warning("Phase 2.8A+2.8B probe_at_startup failed; degrade | err=%s", exc)
        # 兜底:ProbeReport 字段全 False,production_dispatch_forced_off=True
        from app.agent_runtime.persistence import ProbeReport
        probe = ProbeReport(
            postgres_ok=False,
            postgres_url_echo="",
            postgres_latency_ms=0,
            eventbus_kind="InMemoryDegraded",
            eventbus_url_echo="",
            redis_ok=False,
            ready=False,
            production_dispatch_forced_off=True,
            redis_inflight_ok=False,
            redis_inflight_url_echo="",
            in_flight_lock_backend="InMemoryDegraded",
            in_flight_lock_ttl_seconds=settings.agent_runtime_redis_inflight_ttl_seconds,
            warnings=("probe_at_startup raised",),
        )
        app.state.probe_report = probe

    # Phase 2.8B: 跨 worker InFlight Redis lock — 仅当探测 OK 才实例化
    # (空 URL 时 probe 返回 InMemory backend,但 _probe_redis_inflight 不会
    # 创建 registry — 因为没真实 redis_client 可用)。
    if (
        probe.redis_inflight_ok
        and probe.in_flight_lock_backend == "Redis"
        and settings.agent_runtime_redis_inflight_url
    ):
        try:
            import redis.asyncio as redis_async  # type: ignore

            redis_client = redis_async.from_url(
                settings.agent_runtime_redis_inflight_url,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
            redis_inflight = RedisInFlightRegistry(
                redis_client=redis_client,
                ttl_seconds=settings.agent_runtime_redis_inflight_ttl_seconds,
                key_prefix=settings.agent_runtime_redis_inflight_lock_prefix,
            )
            logger.info(
                "Phase 2.8B RedisInFlightRegistry 已就绪 | worker_id=%s ttl=%ds",
                redis_inflight.worker_id,
                redis_inflight.ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Phase 2.8B RedisInFlightRegistry 实例化失败; fallback 到进程级 | err=%s",
                exc,
            )
            redis_inflight = None

    # 2. Checkpointer 选择 — Phase 2.8R-D Fail-Closed。
    #    * Postgres 可用 + 必配置 backend=postgres → AsyncPostgresSaver
    #    * 任何一步失败 → 不退化 MemorySaver,改为把 langgraph 路径 fail-closed
    #      (ProbeReport.langgraph_readiness=False); task creation fails closed.
    cp = None
    is_postgres_cp = False
    checkpointer_error: str | None = None
    try:
        if probe.postgres_ok:
            try:
                cp = await build_postgres_checkpointer(
                    settings.agent_runtime_postgres_url,
                    setup=settings.agent_runtime_postgres_setup_on_start,
                )
            except Exception as exc:
                # Phase 2.8R-D:不再静默 fallback MemorySaver。
                # 把异常记录下来,后续拒挂 LangGraph 路径。
                checkpointer_error = str(exc)
                cp = None
                logger.warning(
                    "Postgres Checkpointer 构建失败;LangGraph 路径 fail-closed | err=%s",
                    exc,
                )
            if cp is not None:
                is_postgres_cp = True
                logger.info(
                    "Phase 2.8A Postgres Checkpointer 已就绪 (pool_size=%d)",
                    settings.agent_runtime_postgres_pool_size,
                )
                # Phase 2.8B 实证:验证 4 张核心表真实存在(env-gate 同 setup 行为:
                # setup_on_start=True 才验证 — 不强制破坏性 fallback)。
                if settings.agent_runtime_postgres_setup_on_start:
                    try:
                        present = await verify_postgres_4_tables(settings.agent_runtime_postgres_url)
                        logger.info(
                            "Phase 2.8B Postgres checkpointer 4-table verify OK: %s",
                            present,
                        )
                    except Exception as exc:  # noqa: BLE001
                        # 4-table 验证失败即关闭执行路径，保持 fail-closed。
                        checkpointer_error = f"verify_postgres_4_tables_failed: {exc}"
                        logger.warning(
                            "Phase 2.8R-D Postgres 4-table verify FAILED; "
                            "langgraph 路径 fail-closed | err=%s",
                            exc,
                        )
                        try:
                            await aclose_postgres_checkpointer(cp)
                        except Exception:
                            pass
                        cp = None
                        is_postgres_cp = False
                        object.__setattr__(probe, "production_dispatch_forced_off", True)
                        object.__setattr__(
                            probe,
                            "warnings",
                            probe.warnings + ("verify_postgres_4_tables_failed",),
                        )
        if cp is None:
            # Phase 2.8R-D:历史代码会在 PG 不可用时退化到 MemorySaver;
            # 现在改为**不构造任何 checkpointer**,后续 graph_runtime_service
            # 使用 None;LangGraph ainvoke 时会按 Postgres 不可用 fail-closed。
            logger.warning(
                "Phase 2.8R-D Checkpointer 未就绪 | "
                "langgraph_dispatch_forced_off=%s | checkpointer_error=%s | "
                "postgres_ok=%s",
                probe.production_dispatch_forced_off,
                checkpointer_error,
                probe.postgres_ok,
            )
        # Phase 2.8R-D:把当前 checkpointer 类型写入 probe。
        # 类型字符串用于 readiness 审计日志。
        try:
            if cp is None:
                cp_type = "none"
            elif is_postgres_cp:
                cp_type = "AsyncPostgresSaver"
            else:
                cp_type = type(cp).__name__
            object.__setattr__(probe, "checkpointer_type", cp_type)
        except Exception:
            pass
        app.state.checkpointer = cp
        app.state.redis_inflight = redis_inflight
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Phase 2.8A build_postgres_checkpointer crashed; use MemorySaver | err=%s",
            exc,
        )
        # 外层兜底:这种情况下 cp 仍是 None,LangGraph 路径 fail-closed。
        app.state.checkpointer = None
        app.state.redis_inflight = redis_inflight

    # CE-04 §二 生产唯一构造链：ContextEngine → Invoker → Bridge。
    # 独立于步骤 3/4 的 try（不推远 LangGraphRunCoordinator 的 except 窗口）。
    # AsyncSessionLocal 必须在 build_production_context_components 调用之前 import，
    # 否则 Python 看到 lifespan 函数体里有 `from app.db.session import AsyncSessionLocal`
    # （原 line 363）会把整个函数内的 AsyncSessionLocal 标记为 local，
    # 导致 line 341 `session_factory=AsyncSessionLocal` 触发 UnboundLocalError。
    from app.db.session import AsyncSessionLocal
    from app.agent_runtime.context_runtime_builder import (
        build_production_context_components,
    )

    _ctx = build_production_context_components(
        session_factory=AsyncSessionLocal,
        settings_service_factory=lambda: getattr(app.state, "settings_service", None),
    )
    app.state.context_engine = _ctx["context_engine"]
    app.state.context_llm_invoker = _ctx["context_llm_invoker"]
    app.state.context_llm_bridge = _ctx["context_llm_bridge"]
    if _ctx["error"]:
        logger.warning("CE-04 Context Runtime degraded | error=%s", _ctx["error"])

    # 3. GraphRuntimeService(include_v2=True, checkpointer=cp)
    #    include_v2=True 是 Phase 2.8A 的新约定:生产 ApiDispatcher 走的
    #    LangGraphRunCoordinator 强制使用 v2 graph,默认 build_default 的
    #    include_v2=False 不再适用;Phase 2.8A 范围内 explicit opt-in。
    try:
        from app.agent_runtime.graph_runtime_service import GraphRuntimeService
        from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
        from app.agent_runtime.langgraph_dispatch_adapter import LangGraphDispatchAdapter
        from app.agent_runtime.langgraph_run_lifecycle import LangGraphRunLifecycle
        from app.agent_runtime.graph_registry import GraphRegistry
        from app.agent_runtime.production_runtime_context_factory import (
            ProductionRuntimeContextFactory,
        )
        # AsyncSessionLocal 已在 lifespan 顶端导入（避免 UnboundLocalError）

        # Phase 2.8R-D:LangGraphRunCoordinator 不再硬编码 v2,而是通过
        # ``_resolve_default_graph_version()`` 读 settings(默认 v3)。
        # registry 同时注册 v1 + v2(v2_frozen)+ v3,确保:
        #   * 历史 v2 任务 reload → v2_frozen(向后兼容)
        #   * 新 LangGraph 任务 → v3(生产默认)
        registry_v2_v3 = GraphRegistry.build_default_v2_v3(checkpointer=cp)
        graph_runtime = GraphRuntimeService(registry=registry_v2_v3)
        app.state.graph_registry = registry_v2_v3
        app.state.graph_runtime = graph_runtime

        # 4. LangGraphRunCoordinator
        runtime_context_factory = ProductionRuntimeContextFactory(
            session_factory=AsyncSessionLocal,
            event_bus_provider=lambda: getattr(app.state, "live_event_bus", None),
            context_engine=app.state.context_engine,
            context_llm_invoker=app.state.context_llm_bridge,
        )
        coordinator = LangGraphRunCoordinator(
            registry=registry_v2_v3,
            runtime=graph_runtime,
            checkpointer=cp,
            context_factory=runtime_context_factory,
        )
        app.state.langgraph_coordinator = coordinator
        app.state.runtime_context_factory = runtime_context_factory

        # 5. LangGraphDispatchAdapter — 把 coordinator 包装成 Protocol
        run_lifecycle = LangGraphRunLifecycle(session_factory=AsyncSessionLocal)
        adapter = LangGraphDispatchAdapter(coordinator, lifecycle=run_lifecycle)
        app.state.langgraph_adapter = adapter
        app.state.langgraph_run_lifecycle = run_lifecycle
    except Exception as exc:  # noqa: BLE001 — coordinator 构造失败不应该阻塞启动
        logger.warning(
            "LangGraphRunCoordinator 构建失败;生产 Agent 执行 fail-closed | err=%s",
            exc,
        )
        graph_runtime = None
        coordinator = None
        adapter = None
        app.state.langgraph_coordinator = None
        app.state.langgraph_adapter = None

    # 6. LangGraph-only ApiDispatcher. It is mounted even when the coordinator
    # failed to initialize so callers receive an explicit runtime-unavailable error.
    # Phase 2.8C:env 透传 dynamic_agent_api_enabled 到 FeatureFlags;
    # ApiDispatcher 构造时若不传 feature_flags,内部 get_feature_flags()
    # 已经自动从 env 读 AGENT_RUNTIME_DYNAMIC_AGENT_API_ENABLED。
    try:
        from app.agent_runtime.api_dispatcher import ApiDispatcher
        from app.agent_runtime.feature_flags import get_feature_flags

        flags = get_feature_flags()
        # Phase 2.8C:env 显式 opt-in 时同步覆盖;否则保持 process 默认
        # (测试 sandbox 自动 True;生产默认 False)。
        if settings.agent_runtime_dynamic_agent_api_enabled:
            import dataclasses
            flags = dataclasses.replace(
                flags, dynamic_agent_api_enabled=True
            )

        api_dispatcher = ApiDispatcher(
            coordinator=adapter,
            probe_report=probe,
            redis_inflight=redis_inflight,  # Phase 2.8B
            feature_flags=flags,
        )
        app.state.api_dispatcher = api_dispatcher
        # Phase 2.8R-D:写 langgraph_readiness 到 probe。计算依据:postgres_ok +
        # registry 含 v3 + coordinator + cp_type=AsyncPostgresSaver。任何缺失 → False。
        try:
            registry_ok = (
                app.state.graph_registry is not None
                and "v3" in (app.state.graph_registry.list_versions("test_plan_generation") or [])
            )
            coordinator_ok = app.state.langgraph_coordinator is not None
            cp_ok = probe.checkpointer_type == "AsyncPostgresSaver"
            # Phase 2.9A.8: 把 ApiDispatcher 自身已挂载作为第四条件;
            # 任一缺失 → readiness=False(Worker 不启动,Agent 端点返 503)。
            dispatcher_ok = api_dispatcher is not None
            langgraph_ready = bool(
                probe.postgres_ok and registry_ok and coordinator_ok
                and cp_ok and dispatcher_ok
                and not probe.production_dispatch_forced_off
            )
            object.__setattr__(probe, "langgraph_readiness", langgraph_ready)
        except Exception as exc:
            logger.warning("Phase 2.8R-D readiness 计算失败: %s", exc)
        logger.info(
            "Phase 2.9A.8 ApiDispatcher built | "
            "dispatcher_type=%s | coordinator_type=%s | "
            "langgraph_readiness=%s | worker_dispatcher_mounted=%s | "
            "flags.prod=%s langgraph=%s dynamic_api=%s | redis_inflight=%s | "
            "dynamic_agents.prep=%s repair=%s incremental=%s | "
            "phase29b.narrative=%s prep=%s repair=%s incremental=%s",
            type(api_dispatcher).__name__,
            type(adapter).__name__ if adapter is not None else "unavailable",
            probe.langgraph_readiness,
            True,
            api_dispatcher.feature_flags.production_dispatch_enabled,
            api_dispatcher.feature_flags.langgraph_enabled,
            api_dispatcher.feature_flags.dynamic_agent_api_enabled,
            "on" if redis_inflight is not None else "off",
            api_dispatcher.feature_flags.preparation_agent_enabled,
            api_dispatcher.feature_flags.repair_agent_enabled,
            api_dispatcher.feature_flags.incremental_agent_enabled,
            api_dispatcher.feature_flags.phase29b_narrative_enabled,
            api_dispatcher.feature_flags.phase29b_preparation_narrative_enabled,
            api_dispatcher.feature_flags.phase29b_repair_narrative_enabled,
            api_dispatcher.feature_flags.phase29b_incremental_narrative_enabled,
        )
    except Exception as exc:  # noqa: BLE001
        # Phase 2.9A.8: 这是关键故障 — ApiDispatcher 挂不上 = 生产入口断了。
        # 严格 fail-closed:读 readiness=False;Worker 启动时检查 readiness,
        # 见到 False 直接不启动 + 不轮询(避免无限暂挂)。
        logger.warning(
            "Phase 2.9A.8 ApiDispatcher 构建失败; production entry broken | err=%s",
            exc,
        )
        app.state.api_dispatcher = None
        # 显式设 False,即便 probe 之前被设为 True 也要覆盖
        try:
            object.__setattr__(probe, "langgraph_readiness", False)
        except Exception:
            pass

    # 7. LiveEventBus(Phase 2.6 已有 init;Step 13 保留兼容,probe 已覆盖探测)
    try:
        from app.agent_runtime.events.live_event_bus import LiveEventBusProbe
        bus = await LiveEventBusProbe.resolve_with_health_check(
            settings.agent_runtime_redis_url or None
        )
        app.state.live_event_bus = bus
        kind = type(bus).__name__
        logger.info("Phase 2.6 LiveEventBus 就绪 | kind=%s", kind)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Phase 2.6 LiveEventBus init failed (use InMemory) | err=%s", exc)

    # 8. Phase 2.8R-B + 2.8R-I + Phase 2.9A.8: AgentExecutionWorker 后台启动
    #    - message_service._create_agent_task 在事务内写 Outbox 行
    #    - Worker 异步 SELECT FOR UPDATE SKIP LOCKED 领取
    #    - 委派到 ApiDispatcher.dispatch_new_task(已挂)
    #    默认 autostart=True;单进程测试可关
    #    Phase 2.9A.8: Fail-Closed — dispatcher 为 None 或 readiness=False
    #    时 Worker 不启动;避免 ``Worker: dispatcher 未挂载`` 无限轮询
    #    + release_lease_for_retry 制造日志噪音。
    app.state.worker_started = False
    if settings.agent_runtime_worker_autostart:
        # Phase 2.9A.8:任何环节未就绪 → 严格不启动
        if app.state.api_dispatcher is None:
            logger.warning(
                "Phase 2.9A.8 AgentExecutionWorker 未启动 | "
                "reason=api_dispatcher_not_mounted | "
                "langgraph_readiness=%s | 不轮询,不暂挂任务",
                probe.langgraph_readiness,
            )
        elif not probe.langgraph_readiness:
            logger.warning(
                "Phase 2.9A.8 AgentExecutionWorker 未启动 | "
                "reason=langgraph_readiness_false | "
                "postgres_ok=%s cp_type=%s | 不轮询,不暂挂任务",
                probe.postgres_ok, probe.checkpointer_type,
            )
        else:
            try:
                from app.services.agent_execution_worker import (
                    AgentExecutionWorker,
                    set_execution_worker,
                )

                worker = AgentExecutionWorker(
                    api_dispatcher=app.state.api_dispatcher,
                    poll_interval_seconds=float(
                        settings.agent_runtime_worker_poll_interval_seconds
                    ),
                )
                await worker.start()
                set_execution_worker(worker)
                app.state.worker_started = True
                logger.info(
                    "Phase 2.9A.8 AgentExecutionWorker started | "
                    "worker_dispatcher_mounted=true | poll_interval=%.1fs | "
                    "langgraph_readiness=%s",
                    worker._poll_interval,
                    probe.langgraph_readiness,
                )
            except Exception as exc:  # noqa: BLE001
                # 失败不阻塞 lifespan,但 readiness 已 False
                logger.warning(
                    "Phase 2.9A.8 AgentExecutionWorker 启动失败; Outbox 队列将无人领 | err=%s",
                    exc,
                )

    pending_legacy_tasks: int | str = "unknown"
    try:
        from sqlalchemy import text as _sa_text

        async with AsyncSessionLocal() as audit_session:
            count_result = await audit_session.execute(
                _sa_text(
                    "SELECT COUNT(*) FROM agent_tasks "
                    "WHERE COALESCE(engine_type, 'legacy') <> 'langgraph' "
                    "AND status NOT IN ('completed', 'failed', 'cancelled') "
                    "AND deleted_at IS NULL"
                )
            )
            pending_legacy_tasks = int(count_result.scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pending historical Legacy task count unavailable | err=%s", exc)
    app.state.pending_legacy_tasks = pending_legacy_tasks

    logger.info(
        "Agent runtime readiness | production_agent_engine=langgraph | "
        "legacy_orchestrator_mounted=false | langgraph_readiness=%s | "
        "worker_started=%s | checkpointer_type=%s | coordinator_mounted=%s | "
        "dispatcher_mounted=%s | pending_legacy_tasks=%s",
        probe.langgraph_readiness,
        app.state.worker_started,
        probe.checkpointer_type,
        adapter is not None,
        app.state.api_dispatcher is not None,
        pending_legacy_tasks,
    )

    # Cache Maintenance Worker (Phase 1 — Redis Cache project)
    # Runs purge_expired_deleted_library + future maintenance jobs at a
    # low cadence (default 60s). Independent of AgentExecutionWorker and
    # ApiDispatcher — must keep running even when the LangGraph path is
    # fail-closed so library purge never stalls. Disabled by the same
    # worker-autostart gate so single-process tests can suppress it.
    if settings.agent_runtime_worker_autostart:
        cache_mw = None
        try:
            from app.services.cache_maintenance_service import CacheMaintenanceWorker

            cache_mw = CacheMaintenanceWorker(
                interval_seconds=settings.cache_maintenance_interval_seconds,
            )
            await cache_mw.start()
            app.state.cache_maintenance_worker = cache_mw
            logger.info(
                "CacheMaintenanceWorker started | interval=%.1fs",
                settings.cache_maintenance_interval_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "CacheMaintenanceWorker 启动失败; library purge 仍由 "
                "AgentExecutionWorker idle 分支兜底 | err=%s", exc,
            )
            app.state.cache_maintenance_worker = None
    else:
        logger.info("CacheMaintenanceWorker 未启动 | agent_runtime_worker_autostart=false")

    # Business Cache Redis backend (Phase 1 — Redis Cache project)
    # 在 CacheMaintenanceWorker 之后挂：CacheBackend 是 lifespan 共享的
    # redis.asyncio client 单例；Domain Cache Service 在后续请求中通过
    # ``get_cache_manager()`` 拿到。Step 3 起真正构造 redis client;
    # 失败（URL 空 / 不可达）→ install 阶段降级 backend（Domain Cache bypass）。
    try:
        from app.cache.backend import CacheBackend, build_redis_client_from_settings
        from app.cache.manager import get_cache_manager

        redis_client = build_redis_client_from_settings()
        await CacheBackend.install(redis_client=redis_client)
        # Eagerly construct manager so first-request latency doesn't pay
        # the lazy-init cost.  Domain Cache flag-gated.
        get_cache_manager()
        backend = CacheBackend.get()
        logger.info(
            "CacheBackend installed | master=%s url_configured=%s "
            "enabled=%s healthy=%s",
            settings.cache_redis_enabled,
            bool(settings.cache_redis_url),
            backend.is_enabled,
            backend.is_healthy,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("CacheBackend install failed; degraded | err=%s", exc)

    # ════════════════════════════════════════════════════════════════════
    # WP-BE-01: 启动 IndexWorker（Context Engine Internal RAG 消费者）。
    #   - 上传 → enqueue（CPS-01 已实现）→ 本 Worker 消费 pending job；
    #   - app restart 后 pending job 可继续被领取（SKIP LOCKED）；
    #   - 受 CONTEXT_INDEX_WORKER_ENABLED flag 控制（.env 默认 1）；
    #   - Index 失败不回滚 Source File（Source Truth 与 Derived Index 分离）；
    #   - graceful shutdown 停止循环，无 leaked task。
    # ════════════════════════════════════════════════════════════════════
    try:
        from app.context_engine.feature_flags import get_context_engine_flags
        from app.context_engine.indexing.worker_factory import (
            build_production_index_worker,
        )

        if get_context_engine_flags().context_index_worker_enabled:
            index_worker = build_production_index_worker(
                session_factory=AsyncSessionLocal,
                poll_interval_seconds=1.0,
            )
            await index_worker.start()
            app.state.index_worker = index_worker
            logger.info(
                "WP-BE-01 IndexWorker started | lease_owner=%s | poll=%.1fs",
                getattr(index_worker, "_lease_owner", "?"),
                getattr(index_worker, "_poll_interval", 1.0),
            )
        else:
            logger.info(
                "WP-BE-01 IndexWorker 未启动 | CONTEXT_INDEX_WORKER_ENABLED=false"
            )
    except Exception as exc:  # noqa: BLE001
        # 失败不阻塞应用启动（RAG 消费降级，上传不影响）
        logger.warning(
            "WP-BE-01 IndexWorker 启动失败; 上传文档将保持 pending | err=%s",
            exc,
        )
        app.state.index_worker = None

    # Context Engine retention runs on a low cadence and is guarded by a
    # connection-bound MySQL advisory lock, so only one app process cleans up.
    app.state.retention_worker = None
    try:
        from app.context_engine.feature_flags import get_context_engine_flags
        from app.context_engine.maintenance.retention_worker import RetentionWorker
        from app.agent_runtime.events.event_retention import EventRetentionPolicy

        if get_context_engine_flags().context_engine_enabled:
            retention_worker = RetentionWorker(
                session_factory=AsyncSessionLocal,
                event_retention_policy_factory=EventRetentionPolicy,
            )
            await retention_worker.start()
            app.state.retention_worker = retention_worker
            logger.info(
                "Context retention worker started | interval=%.1fs | dry_run=%s",
                retention_worker._interval_seconds,
                retention_worker.dry_run,
            )
        else:
            logger.info("Context retention worker not started | CONTEXT_ENGINE_ENABLED=false")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Context retention worker startup failed | err=%s", exc)

    logger.info(
        "应用启动完成 | F015 LLMConfigCache 已就绪 | DB 配置已加载 | 已注册 7 个工具 | 环境=%s",
        "开发" if settings.is_development else "生产",
    )
    log_event(logger, logging.INFO, LogEvent.APPLICATION_STARTUP_COMPLETED, "Application startup completed")
    yield
    log_event(logger, logging.INFO, LogEvent.APPLICATION_SHUTDOWN_STARTED, "Application shutdown started")
    # 优雅关闭 OCR worker 子进程（三阶段：shutdown → terminate → kill）
    try:
        from app.common.ocr_service import ocr_service
        if ocr_service._client is not None:
            ocr_service._client.close()
            logger.info("OCR worker 已关闭")
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR worker 关闭失败: %s", exc)

    # Phase 2.6: 关闭 LiveEventBus
    try:
        bus = getattr(app.state, "live_event_bus", None)
        if bus is not None and hasattr(bus, "aclose"):
            await bus.aclose()
    except Exception:  # noqa: BLE001
        pass

    # Phase 2.8A Step 13: 关闭 Postgres Checkpointer 释放连接池
    try:
        cp = getattr(app.state, "checkpointer", None)
        if cp is not None and is_postgres_cp:
            await aclose_postgres_checkpointer(cp)
            logger.info("Phase 2.8A Postgres Checkpointer 已关闭")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Phase 2.8A Postgres Checkpointer aclose failed: %s", exc)

    # Phase 2.8B: 关闭跨 worker Redis InFlight 锁连接
    try:
        api_d = getattr(app.state, "api_dispatcher", None)
        if api_d is not None and getattr(api_d, "redis_inflight", None) is not None:
            await api_d.redis_inflight.aclose()
            logger.info("Phase 2.8B RedisInFlightRegistry 已关闭")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Phase 2.8B RedisInFlightRegistry aclose failed: %s", exc)

    # Phase 2.8R-B + 2.8R-I: 关闭 AgentExecutionWorker 后台循环
    try:
        from app.services.agent_execution_worker import (
            get_execution_worker,
        )
        w = get_execution_worker()
        if w is not None:
            await w.stop()
            logger.info("Phase 2.8R-B AgentExecutionWorker 已停止")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Phase 2.8R-B AgentExecutionWorker stop failed: %s", exc)

    # Cache Maintenance Worker shutdown (Phase 1 — Redis Cache project)
    try:
        cmw = getattr(app.state, "cache_maintenance_worker", None)
        if cmw is not None:
            await cmw.stop()
            logger.info("CacheMaintenanceWorker 已停止")
    except Exception as exc:  # noqa: BLE001
        logger.warning("CacheMaintenanceWorker stop failed: %s", exc)

    # Business Cache Redis backend shutdown (Phase 1 — Redis Cache project)
    try:
        from app.cache.backend import CacheBackend

        await CacheBackend.aclose()
        logger.info("CacheBackend 已关闭")
    except Exception as exc:  # noqa: BLE001
        logger.warning("CacheBackend aclose failed: %s", exc)

    # WP-BE-01: 关闭 IndexWorker 后台循环（graceful，无 leaked task）
    try:
        retention_worker = getattr(app.state, "retention_worker", None)
        if retention_worker is not None:
            await retention_worker.stop()
            logger.info("Context retention worker stopped")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Context retention worker stop failed: %s", exc)

    try:
        iw = getattr(app.state, "index_worker", None)
        if iw is not None:
            await iw.stop()
            logger.info("WP-BE-01 IndexWorker 已停止")
    except Exception as exc:  # noqa: BLE001
        logger.warning("WP-BE-01 IndexWorker stop failed: %s", exc)

    logger.info("应用关闭")


    log_event(logger, logging.INFO, LogEvent.APPLICATION_SHUTDOWN_COMPLETED, "Application shutdown completed")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition", "Content-Length", "Content-Type", "X-Request-ID"],
    )
    app.add_middleware(RequestLoggingMiddleware)

    # AppError → JSON with correct HTTP status
    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError):
        http_status = _http_status_for_app_error(exc.code)
        return JSONResponse(
            content=error_response(exc.code, exc.message, exc.detail),
            status_code=http_status,
        )

    # HTTPException → unwrap detail if it's our unified envelope, else keep as-is
    from fastapi.exceptions import HTTPException as FastAPIHTTPException
    @app.exception_handler(FastAPIHTTPException)
    async def http_exception_handler(_request: Request, exc: FastAPIHTTPException):
        # If detail is already our unified error dict, return it directly
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            return JSONResponse(content=detail, status_code=exc.status_code)
        # Otherwise wrap in unified format
        code = _http_status_for_app_error(exc.status_code * 100)  # e.g. 401 → 40100
        return JSONResponse(
            content=error_response(code, str(detail)),
            status_code=exc.status_code,
            headers=exc.headers,
        )

    # HTTPException → unified response format
    @app.exception_handler(Exception)
    async def fallback_handler(request: Request, exc: Exception):
        # Let FastAPI's own handlers for HTTPException / validation errors take priority
        # This only catches unexpected exceptions.  ``RequestLoggingMiddleware``
        # stamps correlation onto ``request.scope["state"]`` so we can re-bind
        # it here (ServerErrorMiddleware runs us *outside* the middleware's
        # contextvar block, so the route-level bind is invisible).
        from app.core.logging import bind_log_context, get_request_correlation

        request_id, trace_id = get_request_correlation(request)
        bind_log_context(request_id=request_id, trace_id=trace_id)
        logger.exception("未处理异常 | path=%s", getattr(request, "url", "?"))
        response = JSONResponse(
            content=error_response(50001, "服务器内部错误"),
            status_code=500,
        )
        # ``ServerErrorMiddleware`` sends this response via the *original*
        # ``send`` (bypassing our wrapper), so we must stamp the header here.
        if request_id:
            response.headers["X-Request-ID"] = request_id
        traceparent = (getattr(request, "scope", {}).get("state") or {}).get("traceparent")
        if isinstance(traceparent, str):
            response.headers["traceparent"] = traceparent
        return response

    # Mount v1 API
    app.include_router(v1_router, prefix=settings.api_prefix)

    return app


app = create_app()
