"""AgentRun repository — run-level monitoring (Phase 2.6).

与 ``AgentEventRepository`` 同模式:JSON 列用 raw INSERT + json.dumps,避免
aiomysql 的 dict 参数限制。append_token_usage / finish 用 raw UPDATE 因为
需要在 JSON 列内子键操作(避免读-改-写竞争)。
"""

from __future__ import annotations

import json as _json
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.repositories.base import BaseRepository
from app.utils.datetime import utcnow


class AgentRunRepository(BaseRepository[AgentRun]):
    model = AgentRun

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def create(self, run: AgentRun) -> AgentRun:
        """Insert AgentRun row.

        token_usage_json / error_json 在创建时通常为 None;append 时再写。
        """
        token_str = self._serialize_json(run.token_usage_json)
        error_str = self._serialize_json(run.error_json)

        await self.session.execute(
            text(
                "INSERT INTO agent_runs "
                "(public_id, task_id, engine_type, graph_name, graph_version, "
                " thread_id, status, started_at, finished_at, model_provider, "
                " model_name, token_usage_json, error_json, created_at, updated_at) "
                "VALUES "
                "(:pid, :tid, :et, :gn, :gv, "
                " :thid, :status, :sa, :fa, :mp, "
                " :mn, :tu, :ej, :ca, :ua)"
            ),
            {
                "pid": run.public_id,
                "tid": run.task_id,
                "et": run.engine_type,
                "gn": run.graph_name,
                "gv": run.graph_version,
                "thid": run.thread_id,
                "status": run.status,
                "sa": run.started_at,
                "fa": run.finished_at,
                "mp": run.model_provider,
                "mn": run.model_name,
                "tu": token_str,
                "ej": error_str,
                "ca": run.created_at,
                "ua": run.updated_at,
            },
        )
        await self.session.flush()
        id_result = await self.session.execute(text("SELECT LAST_INSERT_ID()"))
        run.id = id_result.scalar()
        return run

    async def get_by_public_id(self, public_id: str) -> AgentRun | None:
        result = await self.session.execute(
            select(AgentRun).where(AgentRun.public_id == public_id)
        )
        return result.scalar_one_or_none()

    async def get_by_internal_id(self, internal_id: int) -> AgentRun | None:
        result = await self.session.execute(
            select(AgentRun).where(AgentRun.id == internal_id)
        )
        return result.scalar_one_or_none()

    async def list_by_task(
        self, task_internal_id: int, *, limit: int = 20
    ) -> list[AgentRun]:
        """Latest runs first;监控仪表盘查最近 N 个 run。"""
        result = await self.session.execute(
            select(AgentRun)
            .where(AgentRun.task_id == task_internal_id)
            .order_by(AgentRun.started_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def set_status(
        self, *, run_internal_id: int, status: str, error_json: dict | None = None
    ) -> None:
        """原子更新 status + 可选 error_json;updated_at 由 onupdate 自动更新。"""
        params: dict = {"rid": run_internal_id, "st": status, "ua": utcnow()}
        if error_json is not None:
            params["ej"] = _json.dumps(error_json, ensure_ascii=False, default=str)
            await self.session.execute(
                text(
                    "UPDATE agent_runs "
                    "SET status = :st, error_json = :ej, updated_at = :ua "
                    "WHERE id = :rid"
                ),
                params,
            )
        else:
            await self.session.execute(
                text(
                    "UPDATE agent_runs "
                    "SET status = :st, updated_at = :ua "
                    "WHERE id = :rid"
                ),
                params,
            )

    async def finish(
        self, *, run_internal_id: int, status: str, finished_at: datetime
    ) -> None:
        """终态收尾(status ∈ {completed, failed, cancelled});同时写 finished_at。"""
        await self.session.execute(
            text(
                "UPDATE agent_runs "
                "SET status = :st, finished_at = :fa, updated_at = :ua "
                "WHERE id = :rid"
            ),
            {
                "rid": run_internal_id,
                "st": status,
                "fa": finished_at,
                "ua": utcnow(),
            },
        )

    async def append_token_usage(
        self, *, run_internal_id: int, profile: str, prompt_tokens: int, completion_tokens: int
    ) -> dict:
        """在 token_usage_json 内追加一个 profile 子键;返回更新后的 dict。

        路径:token_usage_json.by_profile.<profile> = {
            "prompt_tokens": <int>,
            "completion_tokens": <int>,
            "total_tokens": <int>,
        }
        并同步累加 token_usage_json.total 子键。

        用 JSON_SET 原子更新(MySQL 5.7+),避免读-改-写竞争。
        """
        total = int(prompt_tokens) + int(completion_tokens)
        await self.session.execute(
            text(
                "UPDATE agent_runs "
                "SET token_usage_json = JSON_SET("
                "    COALESCE(token_usage_json, JSON_OBJECT()),"
                "    '$.by_profile', JSON_OBJECT("
                "        :profile_key,"
                "        JSON_OBJECT("
                "            'prompt_tokens', COALESCE("
                "                JSON_EXTRACT(token_usage_json, "
                "                    '$.by_profile.' || :profile_key || '.prompt_tokens'),"
                "                0) + :pt"
                "            ,'completion_tokens', COALESCE("
                "                JSON_EXTRACT(token_usage_json, "
                "                    '$.by_profile.' || :profile_key || '.completion_tokens'),"
                "                0) + :ct"
                "        )"
                "    ),"
                "    '$.total.prompt_tokens', COALESCE("
                "        JSON_EXTRACT(token_usage_json, '$.total.prompt_tokens'), 0) + :pt"
                "    ,'$.total.completion_tokens', COALESCE("
                "        JSON_EXTRACT(token_usage_json, '$.total.completion_tokens'), 0) + :ct"
                "    ,'$.total.total_tokens', COALESCE("
                "        JSON_EXTRACT(token_usage_json, '$.total.total_tokens'), 0) + :total"
                "), "
                "updated_at = :ua "
                "WHERE id = :rid"
            ),
            {
                "rid": run_internal_id,
                "profile_key": profile,
                "pt": int(prompt_tokens),
                "ct": int(completion_tokens),
                "total": total,
                "ua": utcnow(),
            },
        )
        return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": total}

    async def accumulate_cost_estimate(
        self, *, run_internal_id: int, delta_usd: float
    ) -> None:
        """累加 cost_estimate_usd;新 task 的 delta 通常 < $0.01。"""
        if not delta_usd:
            return
        await self.session.execute(
            text(
                "UPDATE agent_runs "
                "SET token_usage_json = JSON_SET("
                "    COALESCE(token_usage_json, JSON_OBJECT()),"
                "    '$.cost_estimate_usd', COALESCE("
                "        JSON_EXTRACT(token_usage_json, '$.cost_estimate_usd'), 0) + :d"
                "), updated_at = :ua "
                "WHERE id = :rid"
            ),
            {"rid": run_internal_id, "d": float(delta_usd), "ua": utcnow()},
        )

    @staticmethod
    def _serialize_json(value: dict | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return _json.dumps(value, ensure_ascii=False, default=str)


# 模块定位:AgentRun 仓储(run-level 监控)
#
# 链路:
#   LangGraphRunCoordinator bind_agent_run → upsert
#   LangGraphRunLifecycle → update 终态
#
# 关键约束:
#   - run_index 在 task_id 内单调增;
#   - token_usage 按 profile 拆 by_profile dict;
#   - final_artifacts 含 public_id,**不**含 storage_path。
