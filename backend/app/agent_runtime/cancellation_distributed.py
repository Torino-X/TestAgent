"""Distributed Cancellation Service — Phase 2.6 多 Worker 协调。

取消来源 worker_A → MySQL ``agent_tasks.runtime_status = 'cancelled'`` 持久化标志
                  → Redis ``pub_cancel:<task_id>`` 实时信号
                  → worker_B 的 ``raise_if_cancelled`` 探测本地 + 周期回查 MySQL

约束(ADR-2.6-5):
  * MySQL 是权威;Redis 是即時信号(可丢)
  * 取消幂等:重复 cancel() 不抛 + 不写第二行事件
  * 取消事件入 ``agent_events``(只是历史审计,不是取消触发源)
  * 与 Legacy ``InMemoryCancellationService`` 不共存——后者仍可作为单 worker fallback

性能:
  * ``is_cancelled`` 优先查本地 cache(本地 worker)
  * cache miss / 显式 refresh 时走 MySQL
  * Redis channel 推 ``{task_id, source, ts}`` payload
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class DistributedCancellationService:
    """MySQL 为权威 + Redis 实时信号双写 的多 worker 取消服务。

    构造参数:
      * ``session_factory`` — ``Callable[[], AsyncContextManager[AsyncSession]]``
      * ``bus`` — ``LiveEventBusProtocol`` 实现(可选;None 时跳过 Redis)
      * ``clock`` — 测试可注入的时钟

    不依赖 LangGraph;不依赖 Phase 2.1+ 的 GraphEventAdapter;Phase 2.0 stub
    协议 ``is_cancelled(task_id)`` 完全保留。
    """

    REDIS_CANCEL_KEY_PREFIX = "pub_cancel:"

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        bus: Optional[Any] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._session_factory = session_factory
        self._bus = bus
        self._clock = clock or _default_clock
        # task_id (str) → True。cache 仅用于本地快速命中;周期回查 MySQL。
        self._cache: dict[str, bool] = {}
        self._lock = threading.Lock()
        self._redis_tasks: dict[str, asyncio.Task] = {}
        self._bus_started = False

    async def cancel(self, *, task_id: str, source: str = "user") -> bool:
        """标记 ``task_id`` 已取消。

        1. UPDATE agent_tasks SET runtime_status='cancelled' WHERE id=:tid
        2. INSERT agent_events event_type='task_cancelled'
        3. Redis pub (if available)

        幂等:已取消时返回 False;首次取消返回 True。
        """
        # 本地快速命中:本地已存在,直接返回 False 避免重复 DB 写
        with self._lock:
            if self._cache.get(str(task_id)):
                return False

        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            tid = None

        if tid is None:
            # 防御:task_id 不可解析时直接返回 False(实际生产不会发生)
            return False

        try:
            async with self._session_factory() as session:
                assert isinstance(session, AsyncSession)
                # 1) UPDATE agent_tasks.runtime_status + .status (CAS 避免覆盖已取消)
                # P0 整改:也把 user-facing ``status`` 一起切到 cancelled,这样
                # SSE / TaskStatusCache 的下一个 read 能立即拿到 cancelled;
                # 然后 commit 完写穿 cache.
                result = await session.execute(
                    text(
                        "UPDATE agent_tasks SET runtime_status = 'cancelled', "
                        "status = 'cancelled' "
                        "WHERE id = :tid AND (runtime_status IS NULL "
                        "OR runtime_status <> 'cancelled')"
                    ),
                    {"tid": tid},
                )
                rowcount = getattr(result, "rowcount", 0) or 0
                if not rowcount:
                    # 已被别处取消:本地缓存并返回 False
                    with self._lock:
                        self._cache[str(task_id)] = True
                    await session.commit()
                    return False

                # 2) 入 agent_events(审计;幂等 dedup 不需要——一次性事件)
                await session.execute(
                    text(
                        "INSERT INTO agent_events "
                        "(public_id, user_id, conversation_id, task_id, "
                        " event_type, message_type, title, content, "
                        " status, event_schema_version, created_at) "
                        "VALUES (:pid, 0, 0, :tid, 'task_cancelled', "
                        " 'task_cancelled', '任务已取消', "
                        " :content, 'created', 1, :now)"
                    ),
                    {
                        "pid": _gen_event_public_id(),
                        "tid": tid,
                        "content": f"task {task_id} cancelled (source={source})",
                        "now": self._clock(),
                    },
                )
                await session.commit()
        except Exception:
            logger.warning(
                "DistributedCancellationService.cancel: DB write failed (swallowed)",
                exc_info=True,
            )
            return False

        # 3) Redis 实时信号(best-effort)
        if self._bus is not None:
            try:
                await self._bus.publish(
                    task_id=str(task_id),
                    event={
                        "event_type": "cancel_observed",
                        "task_id": str(task_id),
                        "source": source,
                        "ts": self._clock().isoformat(),
                    },
                )
            except Exception:
                logger.warning(
                    "DistributedCancellationService.cancel: bus publish failed",
                    exc_info=True,
                )

        # P0 整改:cancellation 通过此路径时(不经过 AgentTaskService.cancel),
        # 也必须让 TaskStatusCache 立刻看到 cancelled 状态,免得 SSE heartbeat
        # 继续返回 running. 仅在 task_id 能解析为 int 的路径生效.
        if tid is not None:
            try:
                # 取 public_id 再写穿 (cache key 是 public_id)
                from sqlalchemy import text as _sa_text
                from app.db.session import AsyncSessionLocal
                from app.cache.domains.task_cache import get_task_status_cache
                from datetime import datetime, timezone

                async with AsyncSessionLocal() as s2:
                    r = await s2.execute(
                        _sa_text("SELECT public_id FROM agent_tasks WHERE id=:tid"),
                        {"tid": int(tid)},
                    )
                    pid_row = r.first()
                if pid_row is not None:
                    pid = pid_row[0]
                    await get_task_status_cache().write_through(
                        task_public_id=pid,
                        status="cancelled",
                        active_run_id=None,
                        updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
            except Exception as cache_exc:  # noqa: BLE001
                logger.debug(
                    "DistributedCancellationService.cancel: task status cache "
                    "write-through failed (swallowed) | tid=%s | %s",
                    tid, cache_exc,
                )

        with self._lock:
            self._cache[str(task_id)] = True
        return True

    async def is_cancelled(self, *, task_id: str) -> bool:
        """幂等查:本地优先 → MySQL 兜底。

        Phase 2.0 协议 ``is_cancelled(task_id: str) -> bool`` 兼容:
            本类的 ``is_cancelled(task_id=...)`` 通过 kwargs 调用;旧 sink
            可能传 ``self._cancellation_service.is_cancelled(str(tid))`` ——
            见 ``is_cancelled_legacy`` 桥接方法。
        """
        key = str(task_id)
        with self._lock:
            cached = self._cache.get(key)
        if cached:
            return True

        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            return False

        try:
            async with self._session_factory() as session:
                row = await session.execute(
                    text(
                        "SELECT runtime_status FROM agent_tasks WHERE id = :tid"
                    ),
                    {"tid": tid},
                )
                rs = row.scalar()
                cancelled = rs == "cancelled"
                if cancelled:
                    with self._lock:
                        self._cache[key] = True
                return cancelled
        except Exception:
            logger.warning(
                "DistributedCancellationService.is_cancelled: DB read failed",
                exc_info=True,
            )
            return False

    # 兼容旧 ``is_cancelled(task_id_str)`` 调用
    def is_cancelled_legacy(self, task_id: str) -> bool:
        """兼容 Phase 2.0 InMemoryCancellationService 协议 call-site.

        旧代码:``self._cancellation_service.is_cancelled(str(tid))``
        此处 str(tid) 用作 key;同步包装:走本地 cache,避免把 sync 路径变 async。
        """
        key = str(task_id)
        with self._lock:
            return bool(self._cache.get(key))

    async def raise_if_cancelled(self, *, task_id: str) -> None:
        """node 内部每次 step 前调用;触发 asyncio.CancelledError 由 orchestrator 处理."""
        if await self.is_cancelled(task_id=task_id):
            raise asyncio.CancelledError(
                f"task {task_id} observed cancellation"
            )

    async def refresh_from_db(self, *, task_id: str) -> bool:
        """强制从 MySQL 拉取并清零 cache;Redis 信号接收后调用。"""
        with self._lock:
            self._cache.pop(str(task_id), None)
        return await self.is_cancelled(task_id=task_id)

    def clear(self, task_id: str) -> None:
        """测试用;清掉本地缓存。"""
        with self._lock:
            self._cache.pop(str(task_id), None)

    async def subscribe_bus(self, *, task_id: str) -> None:
        """订阅 Redis pub_cancel channel;接收信号后 refresh cache。

        单 worker 测试不需要;多 worker 集成测试需要。Phase 2.6 默认不强制启用。
        """
        if self._bus is None:
            return
        if not hasattr(self._bus, "subscribe"):
            return
        # RedisLiveEventBus.subscribe expects asyncio.Queue
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        try:
            await self._bus.subscribe(
                task_id=f"{self.REDIS_CANCEL_KEY_PREFIX}{task_id}",
                queue=q,
            )

            async def _reader() -> None:
                while True:
                    try:
                        _ = await q.get()
                        await self.refresh_from_db(task_id=task_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning(
                            "cancel reader crashed (continuing)", exc_info=True
                        )

            t = asyncio.create_task(_reader(), name=f"cancel-reader:{task_id}")
            with self._lock:
                self._redis_tasks[task_id] = t
        except Exception:
            logger.warning(
                "DistributedCancellationService.subscribe_bus failed",
                exc_info=True,
            )

    async def aclose(self) -> None:
        with self._lock:
            tasks = list(self._redis_tasks.values())
            self._redis_tasks.clear()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _default_clock() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _gen_event_public_id() -> str:
    import uuid as _uuid

    try:
        return f"evt-cancel-{str(_uuid.uuid7())[:18]}"
    except AttributeError:
        return f"evt-cancel-{str(_uuid.uuid4())[:18]}"


__all__ = ["DistributedCancellationService"]


# 模块定位:Distributed Cancellation(Phase 2.6 多 Worker 协调)
#
# 取消来源 worker_A:
#   → MySQL agent_tasks.runtime_status = 'cancelled' 持久化
#   → Redis pub_cancel:<task_id> 实时信号
#   → worker_B 的 raise_if_cancelled 同时探测本地标志 + 周期回查 MySQL
#
# 链路:
#   user 调 api/v1/agent_tasks.cancel → AgentTaskService.cancel_task
#     → MySQL 写 cancelled + Redis publish
#     → 当前 worker 在下一个 checkpoint 触发 raise
#
# 关键约束:
#   - 单 Redis publish 不能保证 worker 收到(可能宕机),所以**必须**周期
#     回查 MySQL 兜底;
#   - Redis 可配置但**不可选**,部署时强制;
#   - 不允许进程内私有标志(避免 cold-restart cancel signal 丢失)。
