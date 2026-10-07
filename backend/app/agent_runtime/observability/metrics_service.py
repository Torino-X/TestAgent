"""CE-05 Metrics Service — 零新增 Migration 聚合查询。

复用现有表（agent_events / llm_context_snapshots / context_compaction_runs /
context_payloads / context_retrieval_runs）做 owner-scope / admin 聚合。
每条 SQL 应用层 asyncio.timeout(5) + MySQL MAX_EXECUTION_TIME 门禁。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

# 默认窗口 / 上限（计划 §5.4）
DEFAULT_WINDOW_MINUTES = 60
MAX_WINDOW_MINUTES = 24 * 60


class MetricsService:
    """Context Metrics 聚合（Owner / Admin scope）。"""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _window(since_minutes: int | None, until_minutes: int | None) -> tuple[datetime, datetime]:
        now = datetime.now(timezone.utc)
        until = now - timedelta(minutes=until_minutes or 0)
        minutes = since_minutes if since_minutes is not None else DEFAULT_WINDOW_MINUTES
        minutes = max(1, min(minutes, MAX_WINDOW_MINUTES))
        since = now - timedelta(minutes=minutes)
        return since, until

    async def owner_overview(self, user_id: int, *, since_minutes: int | None = None) -> dict:
        """Owner 聚合（user_id 过滤）。"""
        since, until = self._window(since_minutes, None)
        out: dict[str, Any] = {"window_minutes": since_minutes or DEFAULT_WINDOW_MINUTES}
        async with self._session_factory() as session:
            # snapshot 量/均 token
            snap = (await session.execute(text(
                """
                SELECT COUNT(*) AS cnt, COALESCE(AVG(estimated_tokens),0) AS avg_tokens
                FROM llm_context_snapshots
                WHERE user_id = :uid AND created_at >= :since AND created_at <= :until
                """
            ), {"uid": user_id, "since": since, "until": until})).one()
            out["snapshots"] = int(snap.cnt or 0)
            out["avg_snapshot_tokens"] = float(snap.avg_tokens or 0)
            # compaction 均 ratio / 计数
            comp = (await session.execute(text(
                """
                SELECT COUNT(*) AS cnt,
                       COALESCE(AVG(compression_ratio),0) AS avg_ratio
                FROM context_compaction_runs
                WHERE user_id = :uid AND created_at >= :since AND created_at <= :until
                """
            ), {"uid": user_id, "since": since, "until": until})).one()
            out["compactions"] = int(comp.cnt or 0)
            out["avg_compression_ratio"] = float(comp.avg_ratio or 0)
            # payload size/status
            payload = (await session.execute(text(
                """
                SELECT COUNT(*) AS cnt, COALESCE(SUM(size_bytes),0) AS total_bytes
                FROM context_payloads
                WHERE user_id = :uid AND created_at >= :since AND created_at <= :until
                """
            ), {"uid": user_id, "since": since, "until": until})).one()
            out["payloads"] = int(payload.cnt or 0)
            out["payload_total_bytes"] = int(payload.total_bytes or 0)
        return out

    async def engine_metrics(self, *, since_minutes: int | None = None) -> dict:
        """Admin 全局 engine 指标（复用 MetricsComparator.compare）。"""
        since, until = self._window(since_minutes, None)
        try:
            from app.agent_runtime.canary.metrics_comparator import MetricsComparator

            comparator = MetricsComparator(session_factory=self._session_factory)
            snapshot = await comparator.compare(
                window_minutes=since_minutes or DEFAULT_WINDOW_MINUTES
            )
            snapshot["context_pipeline"] = await self.context_pipeline_metrics(
                since_minutes=since_minutes
            )
            return snapshot
        except Exception as exc:  # noqa: BLE001
            logger.warning("engine_metrics 聚合失败 | err=%s", exc)
            return {
                "legacy": {},
                "langgraph": {},
                "context_pipeline": await self.context_pipeline_metrics(
                    since_minutes=since_minutes
                ),
            }

    async def context_pipeline_metrics(
        self, *, since_minutes: int | None = None
    ) -> dict[str, Any]:
        """Bounded context pipeline latency, failure, and degradation metrics."""
        since, until = self._window(since_minutes, None)
        async with self._session_factory() as session:
            snapshots = (await session.execute(text(
                "SELECT status, error_code, latency_json FROM llm_context_snapshots "
                "WHERE created_at >= :since AND created_at <= :until ORDER BY id DESC LIMIT 10000"
            ), {"since": since, "until": until})).all()
            retrieval = (await session.execute(text(
                "SELECT status, fallback_code, total_latency_ms FROM context_retrieval_runs "
                "WHERE created_at >= :since AND created_at <= :until ORDER BY id DESC LIMIT 10000"
            ), {"since": since, "until": until})).all()
            compaction = (await session.execute(text(
                "SELECT status, error_code, total_latency_ms FROM context_compaction_runs "
                "WHERE created_at >= :since AND created_at <= :until ORDER BY id DESC LIMIT 10000"
            ), {"since": since, "until": until})).all()

        stage_values: dict[str, list[int]] = {}
        snapshot_failures = 0
        for row in snapshots:
            if row[0] in {"failed", "abandoned"} or row[1]:
                snapshot_failures += 1
            latency = row[2] if isinstance(row[2], dict) else {}
            for stage, value in latency.items():
                if stage.endswith("_ms") and isinstance(value, (int, float)) and value >= 0:
                    stage_values.setdefault(stage, []).append(int(value))

        retrieval_degraded = sum(1 for row in retrieval if row[1])
        retrieval_failures = sum(1 for row in retrieval if row[0] == "failed")
        compaction_failures = sum(1 for row in compaction if row[0] == "failed" or row[1])
        return {
            "window_minutes": since_minutes or DEFAULT_WINDOW_MINUTES,
            "snapshots": {
                "count": len(snapshots),
                "failures": snapshot_failures,
                "failure_rate": _rate(snapshot_failures, len(snapshots)),
                "latency_p95_ms": {stage: _p95(values) for stage, values in stage_values.items()},
            },
            "retrieval": {
                "count": len(retrieval),
                "failures": retrieval_failures,
                "failure_rate": _rate(retrieval_failures, len(retrieval)),
                "degraded": retrieval_degraded,
                "degraded_rate": _rate(retrieval_degraded, len(retrieval)),
                "latency_p95_ms": _p95([int(row[2]) for row in retrieval if row[2] is not None]),
            },
            "compaction": {
                "count": len(compaction),
                "failures": compaction_failures,
                "failure_rate": _rate(compaction_failures, len(compaction)),
                "latency_p95_ms": _p95([int(row[2]) for row in compaction if row[2] is not None]),
            },
        }

    async def trace_owner(
        self, user_id: int, task_public_id: str, *, since_minutes: int | None = None, limit: int = 50
    ) -> list[dict]:
        """Owner Trace：必须带 task_public_id；只返回 public_id/event_type 等。"""
        since, until = self._window(since_minutes, None)
        async with self._session_factory() as session:
            rows = (await session.execute(text(
                """
                SELECT e.public_id, e.event_type, e.sequence_no, e.created_at
                FROM agent_events e
                JOIN agent_tasks t ON t.id = e.task_id
                WHERE e.user_id = :uid AND t.public_id = :tp
                  AND e.created_at >= :since AND e.created_at <= :until
                ORDER BY e.id DESC
                LIMIT :lim
                """
            ), {"uid": user_id, "tp": task_public_id, "since": since, "until": until, "lim": min(limit, 100)})).all()
        return [
            {"public_id": r[0], "event_type": r[1], "sequence_no": r[2],
             "created_at": r[3].isoformat() if r[3] else None}
            for r in rows
        ]

    async def trace_admin(self, task_public_id: str, *, limit: int = 50) -> list[dict]:
        """Return an admin trace without inventing an owner-user filter.

        The owner endpoint intentionally filters by ``e.user_id``.  Reusing it
        with ``user_id=0`` made the documented cross-user admin endpoint always
        return an empty trace for normal tasks.  The authorization boundary is
        enforced by the API router; this query only changes data scope.
        """
        async with self._session_factory() as session:
            rows = (await session.execute(text(
                """
                SELECT e.public_id, e.event_type, e.sequence_no, e.created_at
                FROM agent_events e
                JOIN agent_tasks t ON t.id = e.task_id
                WHERE t.public_id = :tp
                ORDER BY e.id DESC
                LIMIT :lim
                """
            ), {"tp": task_public_id, "lim": min(limit, 100)})).all()
        return [
            {"public_id": r[0], "event_type": r[1], "sequence_no": r[2],
             "created_at": r[3].isoformat() if r[3] else None}
            for r in rows
        ]

    async def dashboard_health(self, *, app_state=None) -> dict:
        """依赖健康（Admin）：probe / 关键依赖状态。"""
        from app.core.config import get_settings

        settings = get_settings()
        env = (getattr(settings, "app_env", "development") or "").strip().lower()
        out = {
            "app_env": env,
            "cursor_secret_configured": bool(getattr(settings, "context_cursor_secret", "")),
            "debug_unlock_configured": bool(os.environ.get("CONTEXT_DEBUG_UNLOCK_SECRET", "")),
            "postgres_checkpointer_configured": bool(getattr(settings, "agent_runtime_postgres_url", "")),
        }
        if app_state is not None:
            out["index_worker"] = _worker_health(getattr(app_state, "index_worker", None))
            out["retention_worker"] = _worker_health(getattr(app_state, "retention_worker", None))
        try:
            async with self._session_factory() as session:
                rows = (await session.execute(text(
                    "SELECT status, COUNT(*) FROM context_index_jobs GROUP BY status"
                ))).all()
            backlog = {str(row[0]): int(row[1]) for row in rows}
            out["index_backlog"] = {
                "by_status": backlog,
                "pending": backlog.get("pending", 0),
                "claimed": backlog.get("claimed", 0),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("dashboard index backlog aggregation failed | err=%s", type(exc).__name__)
            out["index_backlog"] = {"status": "unavailable"}
        return out


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * 0.95)))
    return ordered[index]


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _worker_health(worker) -> dict[str, Any]:
    snapshot = getattr(worker, "health_snapshot", None)
    if callable(snapshot):
        try:
            return snapshot()
        except Exception as exc:  # noqa: BLE001
            return {"running": False, "error_code": f"health.{type(exc).__name__}"}
    return {"running": False, "status": "not_started"}


__all__ = ["MetricsService"]
