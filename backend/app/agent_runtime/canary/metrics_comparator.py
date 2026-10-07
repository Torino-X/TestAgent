"""Phase 2.7 — MetricsComparator 对比 Legacy vs LangGraph 在窗口内的关键指标。

复用 Phase 2.6 已经建立的 ``agent_runs`` + ``agent_events`` + ``agent_tasks``
(engine_type 来源) + ``artifacts`` 表;**不**引入新表(守 ADR-2.7-9)。

输出结构(供 SwitchReadiness 消费)::

    {
      "window_minutes": 60,
      "since": "2026-07-18T12:00:00",
      "until": "2026-07-18T13:00:00",
      "legacy": {
        "task_count": int,
        "success_rate": float,          # 1.0 == 100%
        "avg_token_cost_usd": float,
        "avg_latency_seconds": float,
        "review_pass_rate": float,
        "interrupt_resume_rate": float,
        "duplicate_artifact_count": int,
        "critical_tool_violations": int,
        "sse_loss_rate": float,         # gaps / (max_sequence_no - 1)
      },
      "langgraph": { ... 镜像结构 ... },
      "all_pass": bool,                 # 8 项阈值全过(若调用方已注入 thresholds)
      "gap": { ... 差值 ... }
    }
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)


_METRIC_KEYS = (
    "task_count",
    "success_rate",
    "avg_token_cost_usd",
    "avg_latency_seconds",
    "review_pass_rate",
    "interrupt_resume_rate",
    "duplicate_artifact_count",
    "critical_tool_violations",
    "sse_loss_rate",
)


def _empty_engine_metrics() -> dict:
    return {
        "task_count": 0,
        "success_rate": 0.0,
        "avg_token_cost_usd": 0.0,
        "avg_latency_seconds": 0.0,
        "review_pass_rate": 0.0,
        "interrupt_resume_rate": 0.0,
        "duplicate_artifact_count": 0,
        "critical_tool_violations": 0,
        "sse_loss_rate": 0.0,
    }


@dataclass
class MetricsComparator:
    """Phase 2.7 指标对比器 — 对比 Legacy vs LangGraph 在窗口内的关键指标。

    全部 SQL 通过 ``session_factory()`` 取 fresh session;不跨实例复用连接。
    """

    session_factory: Callable[[], Any]
    clock: Callable[[], datetime] = lambda: datetime.utcnow()

    async def compare(self, *, window_minutes: int = 60) -> dict:
        """执行所有 7 个对比 SQL,组装返回值。

        Args:
            window_minutes: 时间窗口(分钟);默认 60。

        Returns:
            字典镜像 ``legacy`` / ``langgraph`` / ``all_pass`` / ``gap``。
        """
        if window_minutes <= 0:
            raise ValueError("window_minutes must be positive")

        now = self.clock()
        since = now - timedelta(minutes=window_minutes)

        try:
            async with self.session_factory() as session:
                base = await self._aggregate(session, since=since, now=now)
                artifacts = await self._duplicate_artifact_counts(session, since=since)
                violations = await self._critical_tool_violations(session, since=since)
                sse_loss = await self._sse_loss_rates(session, since=since)
        except Exception as exc:
            logger.error("MetricsComparator.compare failed: %s", exc, exc_info=True)
            # 失败时不应阻塞上层告警;返回零值让 caller 判定
            return {
                "window_minutes": window_minutes,
                "since": since.isoformat(),
                "until": now.isoformat(),
                "legacy": _empty_engine_metrics(),
                "langgraph": _empty_engine_metrics(),
                "all_pass": False,
                "gap": {},
                "error": str(exc)[:500],
            }

        legacy = self._attach_extras(
            base.get("legacy", {}), artifacts.get("legacy", 0), violations.get("legacy", 0),
            sse_loss.get("legacy", 0.0),
        )
        langgraph = self._attach_extras(
            base.get("langgraph", {}),
            artifacts.get("langgraph", 0),
            violations.get("langgraph", 0),
            sse_loss.get("langgraph", 0.0),
        )
        gap = self._compute_gap(legacy=legacy, langgraph=langgraph)
        return {
            "window_minutes": window_minutes,
            "since": since.isoformat(),
            "until": now.isoformat(),
            "legacy": legacy,
            "langgraph": langgraph,
            "all_pass": False,  # 评估由 SwitchReadiness 完成;这里不做阈值判断
            "gap": gap,
        }

    @staticmethod
    def _attach_extras(
        base: dict, dup_artifacts: int, violations: int, sse_loss: float
    ) -> dict:
        out = dict(base) if base else _empty_engine_metrics()
        out["duplicate_artifact_count"] = int(dup_artifacts)
        out["critical_tool_violations"] = int(violations)
        out["sse_loss_rate"] = float(sse_loss)
        return out

    @staticmethod
    def _compute_gap(*, legacy: dict, langgraph: dict) -> dict:
        keys = (
            "task_count",
            "success_rate",
            "avg_token_cost_usd",
            "avg_latency_seconds",
            "review_pass_rate",
            "interrupt_resume_rate",
            "duplicate_artifact_count",
            "critical_tool_violations",
            "sse_loss_rate",
        )
        out: dict = {}
        for k in keys:
            lv = legacy.get(k, 0)
            lg = langgraph.get(k, 0)
            out[k] = round(float(lg) - float(lv), 6)
        return out

    # ── SQL primitives ────────────────────────────────────────────────

    async def _aggregate(self, session: Any, *, since: datetime, now: datetime) -> dict:
        """主聚合 SQL,按 ``agent_tasks.engine_type`` 切分。

        注意:``agent_runs.engine_type`` server_default 'langgraph';
        Legacy 不写 run,只看 task.engine_type。
        """
        sql = text(
            """
            SELECT
              t.engine_type                                        AS engine_type,
              COUNT(DISTINCT t.id)                                 AS task_count,
              SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS completed,
              SUM(CASE WHEN t.resume_node IS NOT NULL
                       AND t.status = 'completed' THEN 1 ELSE 0 END) AS resumed,
              SUM(CASE WHEN t.review_result_json IS NOT NULL
                       AND JSON_EXTRACT(t.review_result_json, '$.passed') = TRUE
                       THEN 1 ELSE 0 END)                         AS review_passed,
              AVG(t.review_result_json IS NOT NULL)               AS review_reviewed_rate,
              AVG(TIMESTAMPDIFF(SECOND, t.started_at, t.completed_at)) AS avg_latency_s,
              AVG(
                CASE WHEN t.started_at IS NOT NULL AND t.completed_at IS NOT NULL
                     THEN TIMESTAMPDIFF(SECOND, t.started_at, t.completed_at)
                     ELSE NULL
                END
              )                                                   AS avg_latency_seconds
            FROM agent_tasks t
            WHERE t.created_at >= :since
              AND t.deleted_at IS NULL
            GROUP BY t.engine_type
            """
        )
        rows = await session.execute(sql, {"since": since})
        out: dict = {}
        for row in rows.fetchall():
            engine = (row[0] or "legacy").lower()
            task_count = int(row[1] or 0)
            completed = int(row[2] or 0)
            resumed = int(row[3] or 0)
            review_passed = int(row[4] or 0)
            reviewed_rate = float(row[5] or 0.0)
            avg_latency = float(row[6] or 0.0)
            success_rate = (completed / task_count) if task_count > 0 else 0.0
            resume_rate = (resumed / completed) if completed > 0 else 0.0
            review_pass_rate = (review_passed / task_count) if task_count > 0 else 0.0
            out[engine] = {
                "task_count": task_count,
                "success_rate": round(success_rate, 6),
                "avg_token_cost_usd": 0.0,  # token 单独 SQL
                "avg_latency_seconds": round(avg_latency, 3),
                "review_pass_rate": round(review_pass_rate, 6),
                "review_reviewed_rate": round(reviewed_rate, 6),
                "interrupt_resume_rate": round(resume_rate, 6),
                "duplicate_artifact_count": 0,
                "critical_tool_violations": 0,
                "sse_loss_rate": 0.0,
            }

        token_stats = await self._token_costs(session, since=since)
        for engine, cost in token_stats.items():
            if engine in out:
                out[engine]["avg_token_cost_usd"] = float(cost)
            else:
                out[engine] = _empty_engine_metrics()
                out[engine]["avg_token_cost_usd"] = float(cost)
        return out

    async def _token_costs(self, session: Any, *, since: datetime) -> dict:
        """每引擎平均 token 成本(JSON_EXTRACT from agent_runs.token_usage_json)。"""
        sql = text(
            """
            SELECT
              r.engine_type                                        AS engine_type,
              AVG(
                CAST(JSON_UNQUOTE(JSON_EXTRACT(r.token_usage_json, '$.cost_estimate_usd'))
                     AS DECIMAL(18, 6))
              )                                                   AS avg_cost
            FROM agent_runs r
            WHERE r.started_at >= :since
            GROUP BY r.engine_type
            """
        )
        rows = await session.execute(sql, {"since": since})
        out: dict = {}
        for row in rows.fetchall():
            engine = (row[0] or "langgraph").lower()
            out[engine] = float(row[1] or 0.0)
        return out

    async def _duplicate_artifact_counts(self, session: Any, *, since: datetime) -> dict:
        """每个 engine_type 下重复产出的 artifact 数(>1 行同一 task_id)。

        跨任务可能合理(每个新任务有它自己的 artifact);只关心**单任务**
        内重复写入相同 artifact_id 不该出现 → 用 ``GROUP BY task_id HAVING n>1``。
        """
        sql = text(
            """
            SELECT t.engine_type, COUNT(*) AS dup_count
            FROM (
              SELECT a.task_id, COUNT(*) AS n
              FROM artifacts a
              WHERE a.created_at >= :since
              GROUP BY a.task_id
              HAVING n > 1
            ) dup
            JOIN agent_tasks t ON t.id = dup.task_id
            WHERE t.deleted_at IS NULL
            GROUP BY t.engine_type
            """
        )
        rows = await session.execute(sql, {"since": since})
        out: dict = {"legacy": 0, "langgraph": 0}
        for row in rows.fetchall():
            engine = (row[0] or "legacy").lower()
            out[engine] = int(row[1] or 0)
        return out

    async def _critical_tool_violations(self, session: Any, *, since: datetime) -> dict:
        """严重越权工具调用 = ``tool_calls.error_json.critical=true`` 的次数,按 task engine 拆分。

        防御性处理 schema 不存在时的 fallback:返回 0。
        """
        try:
            sql = text(
                """
                SELECT t.engine_type, COUNT(*) AS n
                FROM tool_calls tc
                JOIN agent_tasks t ON t.id = tc.task_id
                WHERE tc.created_at >= :since
                  AND JSON_EXTRACT(tc.error_json, '$.critical') = TRUE
                GROUP BY t.engine_type
                """
            )
        except Exception:
            return {"legacy": 0, "langgraph": 0}
        try:
            rows = await session.execute(sql, {"since": since})
        except Exception:
            # 表不存在 / 字段缺失 → 默认 0
            return {"legacy": 0, "langgraph": 0}
        out: dict = {"legacy": 0, "langgraph": 0}
        for row in rows.fetchall():
            engine = (row[0] or "legacy").lower()
            out[engine] = int(row[1] or 0)
        return out

    async def _sse_loss_rates(self, session: Any, *, since: datetime) -> dict:
        """SSE 事件丢失率 = 1 - count/max(sequence_no);按 task 算术平均再分 engine。

        简单估算:已发出的 sequence_no 若比 events 数少,意味着有 gap。
        """
        sql = text(
            """
            SELECT t.engine_type, agg.task_id,
                   agg.max_seq, agg.n, agg.n / GREATEST(agg.max_seq, 1) AS coverage
            FROM (
              SELECT e.task_id,
                     MAX(e.sequence_no) AS max_seq,
                     COUNT(*) AS n
              FROM agent_events e
              WHERE e.created_at >= :since
                AND e.sequence_no IS NOT NULL
              GROUP BY e.task_id
            ) agg
            JOIN agent_tasks t ON t.id = agg.task_id
            WHERE t.deleted_at IS NULL
            """
        )
        rows = await session.execute(sql, {"since": since})
        per_engine: dict = {"legacy": [], "langgraph": []}
        for row in rows.fetchall():
            engine = (row[0] or "legacy").lower()
            coverage = float(row[3] or 0.0)
            loss = max(0.0, 1.0 - coverage)
            per_engine.setdefault(engine, []).append(loss)
        return {k: (sum(v) / len(v) if v else 0.0) for k, v in per_engine.items()}

    async def save_artifact(self, *, output_dir: str, payload: Optional[dict] = None) -> str:
        """把 compare() 结果落 JSON + markdown,供运维 review。

        Returns:
            json 文件绝对路径。
        """
        path = Path(output_dir)
        path.mkdir(parents=True, exist_ok=True)
        ts = self.clock().strftime("%Y%m%dT%H%M%SZ")
        json_path = path / f"canary_metrics_{ts}.json"
        md_path = path / f"canary_metrics_{ts}.md"

        json_path.write_text(
            _json.dumps(payload or {}, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        md_path.write_text(_render_markdown(payload or {}), encoding="utf-8")
        return str(json_path)


def _render_markdown(payload: dict) -> str:
    lines = ["# Phase 2.7 — canary metrics snapshot", ""]
    lines.append(f"window_minutes: **{payload.get('window_minutes')}**")
    lines.append(f"since: `{payload.get('since')}`")
    lines.append(f"until: `{payload.get('until')}`")
    lines.append("")
    lines.append("| metric | legacy | langgraph | gap |")
    lines.append("|---|---|---|---|")
    rows = (
        "task_count",
        "success_rate",
        "avg_token_cost_usd",
        "avg_latency_seconds",
        "review_pass_rate",
        "interrupt_resume_rate",
        "duplicate_artifact_count",
        "critical_tool_violations",
        "sse_loss_rate",
    )
    legacy = payload.get("legacy", {})
    langgraph = payload.get("langgraph", {})
    gap = payload.get("gap", {})
    for k in rows:
        lines.append(
            f"| {k} | {legacy.get(k, 0)} | {langgraph.get(k, 0)} | {gap.get(k, 0)} |"
        )
    return "\n".join(lines) + "\n"


__all__ = ["MetricsComparator"]
