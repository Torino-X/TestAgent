"""Sequence Number Allocator — 单任务单调 sequence_no 分配(Phase 2.6)。

设计要点(ADR-2.6-7/19):
  * Redis 路径:``INCR seq:agent_events:<task_id>`` 快速 + 无锁
  * DB 路径:``SELECT COALESCE(MAX(sequence_no), 0) + 1 ... FOR UPDATE`` 兜底
  * ``next_batch(count)`` 预分配 N 个连续序号,避免一事件一锁

幂等:
  * Redis 原子;MySQL 行级锁;两者都保证 sequence_no 单调递增
  * UNIQUE INDEX (task_id, sequence_no) 在历史 replay / 同帧 dedup 场景下
    防止重复分配

约束:
  * redis_client / session_factory 至少一个不能 None;否则抛 ValueError
  * 不预热 — 每次调用即时分配;Redis pipeline 减少 RTT
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class SequenceNumberAllocator:
    """单任务单调 sequence_no 分配器。"""

    def __init__(
        self,
        *,
        redis_client: Any | None = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        if redis_client is None and session_factory is None:
            raise ValueError(
                "SequenceNumberAllocator requires at least one of redis_client or session_factory"
            )
        self._redis = redis_client
        self._session_factory = session_factory

    @staticmethod
    def _key(task_internal_id: int) -> str:
        return f"seq:agent_events:{int(task_internal_id)}"

    async def next(self, *, task_internal_id: int) -> int:
        """返回下一个 sequence_no(单调递增,>=1)。"""
        # Redis 优先
        if self._redis is not None:
            try:
                v = await self._redis.incr(self._key(task_internal_id))
                return int(v)
            except Exception:
                logger.warning(
                    "SequenceNumberAllocator: Redis INCR failed; DB fallback",
                    exc_info=True,
                )
        return await self._db_next(task_internal_id=task_internal_id)

    async def next_batch(
        self, *, task_internal_id: int, count: int
    ) -> list[int]:
        """预分配 N 个连续序号。失败时单步 fallback。"""
        if count <= 0:
            return []
        if self._redis is not None:
            try:
                pipe = self._redis.pipeline()
                for _ in range(int(count)):
                    pipe.incr(self._key(task_internal_id))
                vals = await pipe.execute()
                return [int(v) for v in vals]
            except Exception:
                logger.warning(
                    "SequenceNumberAllocator: Redis pipeline failed; DB fallback",
                    exc_info=True,
                )

        # DB 路径:单 worker 模式不需要行锁
        assert self._session_factory is not None
        async with self._session_factory() as session:
            assert isinstance(session, AsyncSession)
            row = await session.execute(
                text(
                    "SELECT COALESCE(MAX(sequence_no), 0) AS base "
                    "FROM agent_events WHERE task_id = :tid"
                ),
                {"tid": int(task_internal_id)},
            )
            base = int(row.scalar() or 0)
            await session.commit()
            return list(range(base + 1, base + count + 1))

    async def _db_next(self, *, task_internal_id: int) -> int:
        assert self._session_factory is not None
        async with self._session_factory() as session:
            assert isinstance(session, AsyncSession)
            row = await session.execute(
                text(
                    "SELECT COALESCE(MAX(sequence_no), 0) + 1 "
                    "FROM agent_events WHERE task_id = :tid"
                ),
                {"tid": int(task_internal_id)},
            )
            nxt = int(row.scalar() or 1)
            await session.commit()
            return nxt


def compute_idempotency_key(
    *,
    task_id: str,
    graph_run_id: str,
    node_name: str,
    event_type: str,
    sequence_no: int,
) -> str:
    """生成 idempotency_key = "task_id|graph_run_id|node_name|event_type|seq"。

    字段缺失时退化为空字符串(避免 NPE)。长度上限 160 chars。
    """
    parts = [
        str(task_id or ""),
        str(graph_run_id or ""),
        str(node_name or ""),
        str(event_type or ""),
        str(int(sequence_no)),
    ]
    return "|".join(parts)[:160]


__all__ = ["SequenceNumberAllocator", "compute_idempotency_key"]
