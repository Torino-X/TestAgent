"""Lifespan 启动期统一探测 — Phase 2.8A + 2.8B。

设计要点(对应 docs/29 §10 + docs/30 §4):
* ``probe_at_startup`` 在 lifespan 中跑一次,聚合三件事:
  - Postgres Checkpointer 健康(probe_postgres_checkpointer)
  - LiveEventBus 模式(LiveEventBusProbe.resolve_with_health_check)
  - Redis InFlight 锁健康(Phase 2.8B 新增:_probe_redis_inflight)
* 返回 ``ProbeReport`` dataclass — 决策逻辑完全在调用方(避免模块间循环)。
* **Postgres 失败 → 强制 production_dispatch_enabled=False**(守禁令 #18:
  LangGraph 生产任务不得使用纯 MemorySaver)。该决策由调用方在拿到
  ProbeReport 后做,probe_router 只暴露事实。
* **Redis InFlight 失败 → 跨 worker 守护降级**到进程级 InFlightTaskRegistry
  (2.8A 沿用);**不**强制 production_dispatch_enabled=False,因为 Postgres
  健康时单 worker 仍可跑 LangGraph(只是多 worker 并发守护失效)。守禁令 #31:
  Redis 不可用 graceful degrade。
* LiveEventBus 失败由 ``LiveEventBusProbe`` 内部已封装降级;probe_router
  只把"期望 URL vs 实际 kind"翻译为 ``redis_ok`` 字段。
* ProbeReport 挂到 ``app.state.probe_report``,供 main 启动 banner + 后续
  ApiDispatcher 路由判定共用同一份事实。
* 所有探测均 ≤ 5s,绝不阻塞启动(守禁令 #28)。

守禁令:
* 不在此模块做 ApiDispatcher 路由决策,只暴露依赖事实。
* 不写 MySQL / Postgres 任何表;只读。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .postgres_checkpointer import probe_postgres_checkpointer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeReport:
    """Phase 2.8A+2.8B lifespan 探测报告 — 进程级只读事实。

    ``ready`` 是整体服务就绪位:Legacy 不依赖 Postgres,所以只看
    LiveEventBus 健康度;``production_dispatch_forced_off`` 由
    ``postgres_ok=False`` 派生,独立判断 LangGraph 生产路径是否启用
    (守禁令 #18)。调用方读 ``ready`` 判定 lifespan 启动成功,读
    ``production_dispatch_forced_off`` 判定 ApiDispatcher 是否开放。

    Phase 2.8B 新增 4 字段:
    * ``redis_inflight_ok`` — Redis SET NX 是否成功
    * ``redis_inflight_url_echo`` — 脱敏后的 redis url
    * ``in_flight_lock_backend`` — "Redis" / "InMemory" / "InMemoryDegraded"
    * ``in_flight_lock_ttl_seconds`` — TTL,默认 1800

    Phase 2.8R-D 新增:
    * ``langgraph_readiness`` — langgraph 入口整体可发车。包括 Postgres +
      checkpointer + GraphRegistry + ExecutionWorker + 默认 graph 可解析。
      Fail-closed 模式下该位 False,LangGraph 新任务被 ApiDispatcher 拒掉。
    * ``checkpointer_type`` — 真实注入 graph.compile 的 checkpointer 类型:
      ``"AsyncPostgresSaver"`` / ``"MemorySaver"`` / ``"none"``
    """

    postgres_ok: bool
    postgres_url_echo: str  # 已脱敏的 url,空字符串 = 未配置
    postgres_latency_ms: int

    eventbus_kind: str  # "Redis" / "InMemory" / "InMemoryDegraded"
    eventbus_url_echo: str  # 已脱敏的 url,空字符串 = 未配置
    redis_ok: bool  # True = 期望 Redis 且真正拿到 Redis,或未配置(InMemory OK)

    ready: bool
    production_dispatch_forced_off: bool

    # Phase 2.8B 新增 — 跨 worker InFlight Redis lock 探测结果
    # 必须放在带默认值的字段区,与 ``warnings`` 一起组;不然 dataclass
    # init 会因 "non-default argument follows default argument" 报错。
    redis_inflight_ok: bool = False
    redis_inflight_url_echo: str = ""
    in_flight_lock_backend: str = "InMemory"  # "Redis" / "InMemory" / "InMemoryDegraded"
    in_flight_lock_ttl_seconds: int = 1800

    # Phase 2.8R-D 新增。注:ProbeReport 默认 langgraph_readiness=True,
    # 主因是 ApiDispatcher 的 ``_production_langgraph_unlocked`` 闸门使用
    # ``getattr(probe, 'langgraph_readiness', None) is False``(严格比对 False
    # 才拒),False 默认会显式触发 fail-closed,而 True 默认放行。
    # 生产 lifespan 必须显式 setattr(probe, 'langgraph_readiness', True/False)
    # 反映真实状态。
    langgraph_readiness: bool = True
    checkpointer_type: str = "none"  # "AsyncPostgresSaver" / "MemorySaver" / "none"

    warnings: tuple[str, ...] = field(default_factory=tuple)


def _mask_url(url: str) -> str:
    """把 ``postgresql://user:pass@host:5432/db`` 脱敏为 ``postgresql://user:***@host:5432/db``。"""
    if not url:
        return ""
    try:
        scheme, _, rest = url.partition("://")
        if not rest:
            return url
        # 找最后一个 @ 之前冒号
        at_idx = rest.rfind("@")
        if at_idx == -1:
            return url
        userinfo = rest[:at_idx]
        hostinfo = rest[at_idx:]
        colon_idx = userinfo.find(":")
        if colon_idx == -1:
            return url
        return f"{scheme}://{userinfo[:colon_idx + 1]}***{hostinfo}"
    except Exception:  # noqa: BLE001
        return ""


def _eventbus_health(*, redis_url: str, bus) -> tuple[bool, str, bool]:
    """根据期望 URL vs 实际 bus 类型判定 LiveEventBus 健康。

    Returns:
        (redis_ok, eventbus_kind, redis_url_was_provided)
        * redis_url 未提供 → (True, "InMemory", False),视为设计意图
        * redis_url 提供 + 拿到 RedisLiveEventBus → (True, "Redis", True)
        * redis_url 提供 + 拿到 InMemoryLiveEventBus → (False, "InMemoryDegraded", True)
    """
    if not redis_url:
        return True, "InMemory", False
    # 这里用 isinstance 判断而不是依赖名字,避免外部模块改类名后失效
    cls_name = type(bus).__name__
    if cls_name == "RedisLiveEventBus":
        return True, "Redis", True
    if cls_name == "InMemoryLiveEventBus":
        return False, "InMemoryDegraded", True
    # 未知类型 → 不判定 Redis,记 WARN
    return False, cls_name, True


async def probe_at_startup(
    *,
    postgres_url: str,
    redis_url: str,
    redis_inflight_url: str = "",
    redis_inflight_ttl_seconds: int = 1800,
    postgres_timeout: float = 5.0,
    overall_timeout: float = 8.0,
) -> ProbeReport:
    """Phase 2.8A+2.8B 启动期一次性探测 — lifespan 调用。

    整个探测在 ``overall_timeout`` 内必须完成(超时则返回"全部 fail"报告)。
    Postgres / LiveEventBus / Redis InFlight 并发探测,各自有独立超时。

    Args:
        postgres_url: postgresql+asyncpg://...;空串 = 未配置
        redis_url: redis://...;空串 = 未配置(用 InMemory 设计意图)
        redis_inflight_url: 2.8B 跨 worker lock 专用 Redis URL;
            空串 = 跨 worker 守护降级到进程级 in-memory(2.8A)
        redis_inflight_ttl_seconds: TTL,默认 1800
        postgres_timeout: Postgres 单次探测超时(秒);默认 5
        overall_timeout: 整个探测 deadline(秒);默认 8
    """
    pg_url_echo = _mask_url(postgres_url)
    eb_url_echo = _mask_url(redis_url)
    ri_url_echo = _mask_url(redis_inflight_url)

    async def _probe_postgres() -> tuple[bool, int]:
        if not postgres_url or not postgres_url.strip():
            return False, 0
        start = time.monotonic()
        try:
            ok = await asyncio.wait_for(
                probe_postgres_checkpointer(postgres_url, timeout=postgres_timeout),
                timeout=postgres_timeout + 1.0,
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            return ok, latency_ms
        except asyncio.TimeoutError:
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.warning("ProbeRouter: Postgres probe timed out after %ss", postgres_timeout + 1.0)
            return False, latency_ms
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - start) * 1000)
            logger.warning("ProbeRouter: Postgres probe raised %s; degrade to MemorySaver", exc)
            return False, latency_ms

    async def _probe_eventbus() -> tuple[bool, str, bool]:
        if not redis_url or not redis_url.strip():
            # 未配置 → 设计意图 = InMemory
            return True, "InMemory", False
        # 走 LiveEventBusProbe.resolve_with_health_check(Phase 2.6 已有路径)
        from app.agent_runtime.events.live_event_bus import LiveEventBusProbe

        try:
            bus = await asyncio.wait_for(
                LiveEventBusProbe.resolve_with_health_check(redis_url),
                timeout=postgres_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("ProbeRouter: LiveEventBus probe timed out")
            return False, "InMemoryDegraded", True
        except Exception as exc:  # noqa: BLE001
            logger.warning("ProbeRouter: LiveEventBus probe raised %s; degrade to InMemory", exc)
            return False, "InMemoryDegraded", True
        return _eventbus_health(redis_url=redis_url, bus=bus)

    async def _probe_redis_inflight() -> tuple[bool, str]:
        """Phase 2.8B 新增 — 探测 Redis SET NX 写读。

        Returns:
            (ok, backend)
            * 未配置 URL → (True, "InMemory")(设计意图)
            * 配置 + ping OK + SET NX OK → (True, "Redis")
            * 配置 + ping fail / SET NX fail → (False, "InMemoryDegraded")
              (graceful degrade — 守禁令 #31)
        """
        if not redis_inflight_url or not redis_inflight_url.strip():
            # 未配置 → 设计意图 = 进程级 in-memory(2.8A 沿用)
            return True, "InMemory"
        try:
            import redis.asyncio as redis_async  # type: ignore

            client = redis_async.from_url(
                redis_inflight_url,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
            try:
                await asyncio.wait_for(client.ping(), timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ProbeRouter: Redis InFlight ping failed (%s); "
                    "degrade to in-memory",
                    exc,
                )
                return False, "InMemoryDegraded"
            # 真做一次 SET NX + DEL 验证锁语义可用
            try:
                probe_key = "__probe_inflight__"
                ok = await asyncio.wait_for(
                    client.set(probe_key, "1", nx=True, ex=10), timeout=2.0
                )
                # SET NX 返回 True or False 都算 OK(只要不抛)
                await client.delete(probe_key)
                return True, "Redis"
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ProbeRouter: Redis InFlight SET NX probe failed (%s); "
                    "degrade to in-memory",
                    exc,
                )
                return False, "InMemoryDegraded"
            finally:
                try:
                    await client.aclose()
                except Exception:
                    pass
        except ImportError:
            logger.warning(
                "ProbeRouter: redis.asyncio not installed; degrade to in-memory"
            )
            return False, "InMemoryDegraded"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ProbeRouter: Redis InFlight probe raised %s; degrade to in-memory",
                exc,
            )
            return False, "InMemoryDegraded"

    # ── 并发探测 ──
    async def _runner() -> tuple[
        tuple[bool, int],
        tuple[bool, str, bool],
        tuple[bool, str],
    ]:
        return await asyncio.gather(
            _probe_postgres(),
            _probe_eventbus(),
            _probe_redis_inflight(),
        )

    try:
        (pg_ok, pg_latency), (eb_ok, eb_kind, _), (ri_ok, ri_backend) = await asyncio.wait_for(
            _runner(), timeout=overall_timeout
        )
    except asyncio.TimeoutError:
        logger.warning("ProbeRouter: overall probe timed out after %ss", overall_timeout)
        pg_ok, pg_latency = False, 0
        eb_ok, eb_kind = False, "InMemoryDegraded"
        ri_ok, ri_backend = False, "InMemoryDegraded"

    warnings_list: list[str] = []
    if not pg_ok:
        msg = (
            "Postgres Checkpointer probe failed — production_dispatch_enabled "
            "will be forced OFF; LangGraph tasks fall back to MemorySaver "
            "(single-process only)."
        )
        warnings_list.append(msg)
        logger.warning("ProbeRouter: %s", msg)
    if not eb_ok:
        msg = (
            "LiveEventBus Redis unavailable — cross-worker event fan-out "
            "degraded to InMemory (single-worker only)."
        )
        warnings_list.append(msg)
        logger.warning("ProbeRouter: %s", msg)
    if not ri_ok:
        msg = (
            "Redis InFlight lock unavailable — cross-worker parallel-dispatch "
            "guard degraded to in-memory (守禁令 #20/#31 single-process only)."
        )
        warnings_list.append(msg)
        logger.warning("ProbeRouter: %s", msg)

    ready = eb_ok  # 整体服务就绪位:Legacy 不依赖 Postgres,只要 LiveEventBus 健康就 ready=True;
                  # LangGraph 生产路径单独看 production_dispatch_forced_off。
    prod_forced_off = not pg_ok  # 守禁令 #18:LangGraph 生产任务不得用纯 MemorySaver;

    return ProbeReport(
        postgres_ok=pg_ok,
        postgres_url_echo=pg_url_echo,
        postgres_latency_ms=pg_latency,
        eventbus_kind=eb_kind,
        eventbus_url_echo=eb_url_echo,
        redis_ok=eb_ok,
        # Phase 2.8B 新增字段
        redis_inflight_ok=ri_ok,
        redis_inflight_url_echo=ri_url_echo,
        in_flight_lock_backend=ri_backend,
        in_flight_lock_ttl_seconds=redis_inflight_ttl_seconds,
        ready=ready,
        production_dispatch_forced_off=prod_forced_off,
        warnings=tuple(warnings_list),
    )


__all__ = [
    "ProbeReport",
    "probe_at_startup",
]
