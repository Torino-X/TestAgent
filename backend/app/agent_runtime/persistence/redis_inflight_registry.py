"""Phase 2.8B 跨 worker 并行调度守护 — Redis SET NX + TTL。

设计要点(对应 docs/30 §3 + ADR-2.8B-1/4/8):

* **互斥锁** = ``Redis SET NX EX <ttl>``(原子写)。同一 ``task_public_id`` 的
  第二个 acquire 返回 ``False`` → 抛 ``ParallelDispatchGuardError``(2.8A
  沿用)。
* **Owner 校验** = value 写 ``InflightOwner`` JSON;``release`` 时校验
  ``worker_id`` 一致才 DEL,防误删(老 worker 释放新 worker 的锁)。
* **TTL 兜底** = 锁默认 30 分钟过期;worker 崩溃后其他 worker 接管无需
  人工清理。``ttl_seconds`` 可调。
* **Graceful degrade** = Redis 不可用(``redis_client=None`` / SET 抛异常
  / ping 失败)时 ``try_acquire`` 返回 ``None``,``release`` 返回 ``False``;
  ``ApiDispatcher`` 调用方应 fallback 到进程级 ``InFlightTaskRegistry`` +
  WARN 日志(守禁令 #31)。
* **跨 asyncio task 安全** = 同一 ``redis_client``(真实 Redis 或
  ``fakeredis.aioredis.FakeRedis``)可被多 asyncio task 共享;Redis 的
  命令序列化保证原子性。
* **f-string key** = ``f"{prefix}{task_public_id}"``(默认 prefix
  ``"inflight:"``),与 LiveEventBus / DistributedCancellationService
  使用的 keyspace 隔离。

守禁令映射:
* #20 双引擎并行 →  ``ParallelDispatchGuardError`` 跨进程版本
* #31 Redis 不可用不 raise 5xx → try_acquire 返回 None,release 返回 False
* #32 owner 校验失败不阻断 → release 仅 WARN,不 raise

不复用 ``DistributedCancellationService``:本场景 lock 必须 Redis 实时,
不能用 MySQL 兜底(否则失去"跨 worker 立即可见"语义)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

REDIS_INFLIGHT_KEY_PREFIX = "inflight:"


@dataclass(frozen=True)
class InflightOwner:
    """持有者身份 — ``release`` 时用于 owner 校验。

    ``worker_id`` 默认格式 ``f"{HOSTNAME}-{pid}-{uuid4().hex[:6]}"``;
    测试可通过 ctor 传固定值以模拟多 worker。
    """

    worker_id: str
    engine: str
    acquired_at: float


def _gen_worker_id() -> str:
    """生成 worker_id — 默认 ``f"{HOSTNAME}-{pid}-{uuid4().hex[:6]}"``。"""
    hostname = os.getenv("HOSTNAME", "unknown")
    pid = os.getpid()
    suffix = uuid.uuid4().hex[:6]
    return f"{hostname}-{pid}-{suffix}"


class RedisInFlightRegistry:
    """跨 worker InFlight 守护 — 独立 Protocol,与进程级 ``InFlightTaskRegistry`` 并存。

    ``ApiDispatcher`` 同时持有两个 registry:
    * ``self._inflight`` — 进程级 in-memory(2.8A 守禁令 #20 单进程场景)
    * ``self._redis_inflight`` — 跨 worker Redis SET NX(2.8B 新增)

    双层守护任一抛 ``ParallelDispatchGuardError`` 即拒绝 dispatch。

    用法::

        reg = RedisInFlightRegistry(redis_client=redis)
        owner = await reg.try_acquire("task-1", "langgraph")
        if owner is None:
            # Redis 不可用 → fallback to in-memory
            ...
        else:
            try:
                await do_work()
            finally:
                await reg.release("task-1", owner)
    """

    def __init__(
        self,
        *,
        redis_client: Optional[Any] = None,
        ttl_seconds: int = 1800,
        key_prefix: str = REDIS_INFLIGHT_KEY_PREFIX,
        worker_id: str = "",
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds
        self._prefix = key_prefix
        self._worker_id = worker_id or _gen_worker_id()
        self._clock = clock or _default_clock

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    @property
    def key_prefix(self) -> str:
        return self._prefix

    def _key(self, task_public_id: str) -> str:
        return f"{self._prefix}{task_public_id}"

    async def try_acquire(
        self, task_public_id: str, engine: str
    ) -> Optional[InflightOwner]:
        """尝试跨 worker 获取锁。

        Returns:
            ``InflightOwner`` 当成功获得锁;
            ``None`` 当 Redis 不可用(调用方应 fallback 到 in-memory);

        Raises:
            ``ParallelDispatchGuardError`` 当同 task_public_id 已存在锁
            (其他 worker 在跑)。``running_engine`` 取自 Redis 中已存
            owner 的 engine 字段。
        """
        if self._redis is None:
            return None

        owner = InflightOwner(
            worker_id=self._worker_id,
            engine=engine,
            acquired_at=self._clock(),
        )
        key = self._key(task_public_id)
        payload = json.dumps(owner.__dict__)
        try:
            acquired = await self._redis.set(key, payload, nx=True, ex=self._ttl)
        except Exception as exc:  # noqa: BLE001 — 守禁令 #31
            logger.warning(
                "RedisInFlightRegistry.try_acquire: Redis SET failed (%s); "
                "degrade to in-memory",
                exc,
            )
            return None

        if not acquired:
            # 已被其他 worker 持有 — 读 owner 用于错误信息
            running_engine = "<unknown>"
            try:
                existing = await self._redis.get(key)
                if existing:
                    if isinstance(existing, bytes):
                        existing = existing.decode("utf-8")
                    running_engine = json.loads(existing).get("engine", "<unknown>")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "RedisInFlightRegistry.try_acquire: existing owner read failed (%s)",
                    exc,
                )

            # 局部 import 避免循环
            from app.agent_runtime.dispatch_errors import ParallelDispatchGuardError

            raise ParallelDispatchGuardError(
                task_public_id=task_public_id,
                running_engine=running_engine,
                requested_engine=engine,
            )

        return owner

    async def release(
        self, task_public_id: str, owner: Optional[InflightOwner]
    ) -> bool:
        """释放跨 worker 锁。校验 owner 防误删。

        Returns:
            ``True`` 当成功 DEL 自己的锁;
            ``False`` 当 Redis 不可用 / owner mismatch / key 已 TTL 过期;

        不抛 — TTL 兜底过期(守禁令 #32)。
        """
        if self._redis is None or owner is None:
            return False

        key = self._key(task_public_id)
        try:
            existing = await self._redis.get(key)
            if not existing:
                return False
            if isinstance(existing, bytes):
                existing = existing.decode("utf-8")
            cur = json.loads(existing)
            if cur.get("worker_id") != owner.worker_id:
                logger.warning(
                    "RedisInFlightRegistry.release: owner mismatch task=%s "
                    "holder=%s self=%s; TTL will reclaim",
                    task_public_id,
                    cur.get("worker_id"),
                    owner.worker_id,
                )
                return False
            await self._redis.delete(key)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "RedisInFlightRegistry.release: Redis DEL failed (%s); "
                "TTL will reclaim",
                exc,
            )
            return False

    async def aclose(self) -> None:
        """关闭 Redis 连接(若有)。"""
        if self._redis is not None and hasattr(self._redis, "aclose"):
            try:
                await self._redis.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("RedisInFlightRegistry.aclose failed: %s", exc)


def _default_clock() -> float:
    """跨 asyncio loop 的稳定 monotonic clock。

    ``asyncio.get_event_loop().time()`` 在 asyncio.run 启动后会绑定当前
    loop;在 sync 上下文下会 fallback 到 ``time.monotonic()``。
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return loop.time()
    except RuntimeError:
        pass
    import time as _time

    return _time.monotonic()


__all__ = [
    "InflightOwner",
    "REDIS_INFLIGHT_KEY_PREFIX",
    "RedisInFlightRegistry",
]