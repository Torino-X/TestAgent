"""Run Monitor — Phase 2.6 run 级别 orchestrator。

API:
  * ``RunMonitor.start(...)`` → 新建 AgentRun 行(status='running')
  * ``RunMonitor.append_tokens(...)`` → atomic token_usage_json 累加
  * ``RunMonitor.finish(...)`` → status ∈ {completed, failed, cancelled}

Error capture:status == 'failed' 时 ``error_json`` 字段收 ``{code, message, traceback_no}``。

生命周期边界:
  * 全部失败仅 log,不抛(节点不应因 monitor 失败失败)
  * 不阻塞 LangGraph 主图 — 不在事件循环里做长事务
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.repositories.agent_run_repository import AgentRunRepository
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)


@dataclass
class TokenUsageRow:
    """单 profile 的 token usage 累计快照。"""

    profile: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return int(self.prompt_tokens) + int(self.completion_tokens)


class RunMonitor:
    """Phase 2.6 run 级监控 — 只关心 agent_runs 表,不掺 GraphState。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or _default_clock

    async def start(
        self,
        *,
        task_internal_id: int,
        engine_type: str = "langgraph",
        graph_name: Optional[str] = None,
        graph_version: Optional[str] = None,
        thread_id: Optional[str] = None,
        model_provider: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> AgentRun | None:
        """新建 AgentRun,status='running';返回新行 ORM 对象(已 flush)。"""
        try:
            now = self._clock()
            run = AgentRun(
                public_id=generate_public_id("run"),
                task_id=int(task_internal_id),
                engine_type=engine_type,
                graph_name=graph_name,
                graph_version=graph_version,
                thread_id=thread_id,
                status="running",
                started_at=now,
                model_provider=model_provider,
                model_name=model_name,
                created_at=now,
                updated_at=now,
            )
            async with self._session_factory() as session:
                repo = AgentRunRepository(session)
                created = await repo.create(run)
                await session.commit()
                return created
        except Exception:
            logger.warning(
                "RunMonitor.start failed (swallowed; monitor best-effort)",
                exc_info=True,
            )
            return None

    async def append_tokens(
        self,
        *,
        run_public_id: str,
        profile: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """累计单 profile token usage;token_usage_json 子键 by_profile.<profile>。"""
        try:
            async with self._session_factory() as session:
                repo = AgentRunRepository(session)
                got = await repo.get_by_public_id(run_public_id)
                if got is None:
                    return
                await repo.append_token_usage(
                    run_internal_id=int(got.id),
                    profile=profile,
                    prompt_tokens=int(prompt_tokens),
                    completion_tokens=int(completion_tokens),
                )
                await session.commit()
        except Exception:
            logger.warning(
                "RunMonitor.append_tokens failed (swallowed)", exc_info=True
            )

    async def finish(
        self,
        *,
        run_public_id: str,
        success: bool,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        error_traceback: Optional[str] = None,
    ) -> None:
        """status 终态收尾。

        success=True:status='completed',error_json=None
        success=False:status='failed',error_json={'code','message','traceback_no'}
        """
        try:
            status = "completed" if success else "failed"
            err_dict = (
                None
                if success
                else {
                    "code": str(error_code or "unknown"),
                    "message": str(error_message or "")[:500],
                    "traceback_no": str(error_traceback or "")[:2000],
                }
            )
            async with self._session_factory() as session:
                repo = AgentRunRepository(session)
                got = await repo.get_by_public_id(run_public_id)
                if got is None:
                    return
                await repo.set_status(
                    run_internal_id=int(got.id),
                    status=status,
                    error_json=err_dict,
                )
                await repo.finish(
                    run_internal_id=int(got.id),
                    status=status,
                    finished_at=self._clock(),
                )
                await session.commit()
        except Exception:
            logger.warning(
                "RunMonitor.finish failed (swallowed)", exc_info=True
            )

    async def list_node_metrics(self, task_internal_id: int) -> list[dict]:
        """从 agent_events.node_name 聚合 node 级耗时 / 计数。

        SELECT node_name, COUNT(*) AS events,
               MIN(created_at) AS first, MAX(created_at) AS last
        FROM agent_events WHERE task_id=:tid AND node_name IS NOT NULL
        GROUP BY node_name ORDER BY MIN(created_at) ASC
        """
        from sqlalchemy import text

        try:
            async with self._session_factory() as session:
                rows = await session.execute(
                    text(
                        "SELECT node_name, COUNT(*) AS n, "
                        "       MIN(created_at) AS first_at, "
                        "       MAX(created_at) AS last_at "
                        "FROM agent_events "
                        "WHERE task_id = :tid AND node_name IS NOT NULL "
                        "GROUP BY node_name "
                        "ORDER BY MIN(created_at) ASC"
                    ),
                    {"tid": int(task_internal_id)},
                )
                return [
                    {
                        "node_name": r[0],
                        "event_count": int(r[1]),
                        "first_at": str(r[2]),
                        "last_at": str(r[3]),
                    }
                    for r in rows.fetchall()
                ]
        except Exception:
            logger.warning(
                "RunMonitor.list_node_metrics failed (swallowed)", exc_info=True
            )
            return []


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


__all__ = ["RunMonitor", "TokenUsageRow"]
