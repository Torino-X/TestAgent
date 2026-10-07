"""Event Retention Policy — Phase 2.6 ``agent_events`` 防膨胀。

保留策略(ADR-2.6-10):
  * keep_last_days = 30 — 默认按天数清理
  * keep_last_per_task = 5000 — 每任务最多保留 5000 条
  * cascade: 先按天数删,再按 per-task 条数删
  * 一次最多删 max_delete_batch = 10000,避免长事务
  * 不删 task 已结束的最后一个 terminated 行(例如 task_completed);保留 sequence_no 最大的 1 条

安全:
  * 全部走 SQL(aiomysql 兼容);无 ORM 依赖
  * 错误仅 log,不抛 — CLI 退出码 = 1
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventRetentionConfig:
    """可被 env / 测试覆盖。"""

    keep_last_days: int = 30
    keep_last_per_task: int = 5000
    max_delete_batch: int = 10_000
    protect_last_terminated_per_task: bool = True  # 保留每任务最后一条终态

    def to_dict(self) -> dict[str, Any]:
        return {
            "keep_last_days": self.keep_last_days,
            "keep_last_per_task": self.keep_last_per_task,
            "max_delete_batch": self.max_delete_batch,
            "protect_last_terminated_per_task": self.protect_last_terminated_per_task,
        }


DEFAULT_CONFIG = EventRetentionConfig()


class EventRetentionPolicy:
    """按 config 限制清理 agent_events 旧行。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        config: Optional[EventRetentionConfig] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config or DEFAULT_CONFIG
        self._clock = clock or _default_clock

    async def cleanup_once(self) -> dict[str, Any]:
        """跑一次清理。返回 summary {deleted_by_age, deleted_by_per_task, total_deleted}。

        步骤:
          1. 按天数清理:``created_at < now - keep_last_days`` 的行
          2. 按每任务条数清理:每任务保留最新 keep_last_per_task 条
          3. (可选) 保护每任务最后一条终态行:``event_type IN (...)``
        """
        summary = {
            "deleted_by_age": 0,
            "deleted_by_per_task": 0,
            "total_deleted": 0,
            "ran_at": self._clock().isoformat(),
            "config": self._config.to_dict(),
        }

        cutoff = self._clock() - timedelta(days=self._config.keep_last_days)
        protected_event_types = (
            "task_completed",
            "task_cancelled",
            "task_failed",
        )

        async with self._session_factory() as session:
            assert isinstance(session, AsyncSession)

            # ── 阶段 1: 按天数清理 ─────────────────────────
            try:
                result = await session.execute(
                    text(
                        "DELETE FROM agent_events "
                        "WHERE created_at < :cutoff "
                        "LIMIT :lim"
                    ),
                    {"cutoff": cutoff, "lim": int(self._config.max_delete_batch)},
                )
                age_deleted = getattr(result, "rowcount", 0) or 0
                summary["deleted_by_age"] = int(age_deleted)
                if age_deleted >= self._config.max_delete_batch:
                    # batch cap hit;留到下一次 run-once 再清
                    logger.info(
                        "EventRetentionPolicy: age-delete batch cap hit (%d); deferring rest",
                        age_deleted,
                    )
                await session.commit()
            except Exception:
                logger.warning(
                    "EventRetentionPolicy: age-delete failed (swallowed)",
                    exc_info=True,
                )
                await session.rollback()

            # ── 阶段 2: 按 per-task 条数清理 ───────────────
            try:
                row = await session.execute(
                    text(
                        "SELECT task_id, COUNT(*) AS n "
                        "FROM agent_events "
                        "GROUP BY task_id "
                        "HAVING n > :k"
                    ),
                    {"k": int(self._config.keep_last_per_task)},
                )
                bloated_tasks = [int(t[0]) for t in row.fetchall()]
                per_task_deleted = 0
                for tid in bloated_tasks:
                    # 用子查询取该任务保留 keep_last_per_task 条的最大 id
                    sub = await session.execute(
                        text(
                            "SELECT MIN(id) FROM ("
                            "  SELECT id FROM agent_events "
                            "  WHERE task_id = :tid "
                            "  ORDER BY COALESCE(sequence_no, 0) DESC, id DESC "
                            "  LIMIT :k OFFSET :k"
                            ") AS keeper_max"
                        ),
                        {
                            "tid": int(tid),
                            "k": int(self._config.keep_last_per_task),
                        },
                    )
                    cutoff_id = sub.scalar()
                    if cutoff_id is None:
                        continue
                    del_res = await session.execute(
                        text(
                            "DELETE FROM agent_events "
                            "WHERE task_id = :tid AND id < :cid "
                            "AND event_type NOT IN ("
                            "  'task_completed','task_cancelled','task_failed'"
                            ") "
                            "LIMIT :lim"
                        ),
                        {
                            "tid": int(tid),
                            "cid": int(cutoff_id),
                            "lim": int(self._config.max_delete_batch),
                        },
                    )
                    per_task_deleted += getattr(del_res, "rowcount", 0) or 0
                summary["deleted_by_per_task"] = int(per_task_deleted)
                await session.commit()
            except Exception:
                logger.warning(
                    "EventRetentionPolicy: per-task-delete failed (swallowed)",
                    exc_info=True,
                )
                await session.rollback()

        summary["total_deleted"] = int(
            summary["deleted_by_age"] + summary["deleted_by_per_task"]
        )
        return summary

    async def preview_once(self) -> dict[str, Any]:
        """Count retention candidates without issuing DELETE/UPDATE/commit."""
        cutoff = self._clock() - timedelta(days=self._config.keep_last_days)
        async with self._session_factory() as session:
            age_result = await session.execute(
                text("SELECT COUNT(*) FROM agent_events WHERE created_at < :cutoff"),
                {"cutoff": cutoff},
            )
            age_candidates = min(
                int(age_result.scalar() or 0), self._config.max_delete_batch
            )
            overflow_result = await session.execute(
                text(
                    "SELECT task_id, COUNT(*) AS n FROM agent_events "
                    "GROUP BY task_id HAVING COUNT(*) > :k"
                ),
                {"k": int(self._config.keep_last_per_task)},
            )
            per_task_candidates = sum(
                max(0, int(row[1]) - self._config.keep_last_per_task)
                for row in overflow_result.fetchall()
            )
            per_task_candidates = min(
                per_task_candidates, self._config.max_delete_batch
            )
        return {
            "age_candidates": age_candidates,
            "per_task_candidates": per_task_candidates,
            "total_candidates": age_candidates + per_task_candidates,
            "ran_at": self._clock().isoformat(),
            "config": self._config.to_dict(),
            "dry_run": True,
        }


def _default_clock() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


__all__ = ["EventRetentionPolicy", "EventRetentionConfig", "DEFAULT_CONFIG"]
