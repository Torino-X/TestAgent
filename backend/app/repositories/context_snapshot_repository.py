"""Context snapshot repository."""

from __future__ import annotations

import json as _json

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.context_snapshot import ContextSnapshot
from app.repositories.base import BaseRepository


class ContextSnapshotRepository(BaseRepository[ContextSnapshot]):
    model = ContextSnapshot

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def create(self, snapshot: ContextSnapshot) -> ContextSnapshot:
        # aiomysql driver chokes on dict params for JSON columns —
        # use raw INSERT with explicit json.dumps.
        included_msg = self._serialize_json(snapshot.included_message_ids)
        included_files = self._serialize_json(snapshot.included_file_ids)
        included_tasks = self._serialize_json(snapshot.included_task_ids)
        included_artifacts = self._serialize_json(snapshot.included_artifact_ids)
        included_knowledge = self._serialize_json(snapshot.included_knowledge_ids)

        await self.session.execute(
            text(
                "INSERT INTO llm_context_snapshots "
                "(public_id, user_id, conversation_id, message_id, agent_task_id, "
                " llm_task_type, context_kind, "
                " included_message_ids, included_file_ids, "
                " included_task_ids, included_artifact_ids, included_knowledge_ids, "
                " summary_id, estimated_tokens, context_digest, context_preview, "
                " engine_version, call_site, context_profile_key, context_profile_version, "
                " context_policy_version, model_config_id, model_name_snapshot, "
                " context_window_tokens, input_budget_tokens, output_reserve_tokens, "
                " runtime_reserve_tokens, safety_margin_tokens, target_input_tokens, "
                " estimated_input_tokens, actual_input_tokens, actual_output_tokens, "
                " section_stats_json, included_refs_json, dropped_refs_json, "
                " retrieval_run_ids_json, compaction_json, tool_output_json, "
                " fallback_json, latency_json, prompt_digest, prompt_excerpt, "
                " full_prompt_payload_id, status, error_code, completed_at, "
                " created_at) "
                "VALUES "
                "(:pid, :uid, :cid, :mid, :tid, "
                " :ltt, :ck, "
                " :imi, :ifi, "
                " :iti, :iai, :iki, "
                " :sid, :et, :cd, :cp, "
                " :ev, :cs, :cpk, :cpv, "
                " :cpol, :mci, :mns, "
                " :cwt, :ibt, :ort, "
                " :rrt, :sm, :tit, "
                " :eit, :ait, :aot, "
                " :ssj, :irj, :drj, "
                " :rrj, :coj, :toj, "
                " :fbj, :lat, :pd, :pe, "
                " :fpp, :st, :ec, :ca, "
                " :ct)"
            ),
            {
                "pid": snapshot.public_id,
                "uid": snapshot.user_id,
                "cid": snapshot.conversation_id,
                "mid": snapshot.message_id,
                "tid": snapshot.agent_task_id,
                "ltt": snapshot.llm_task_type,
                "ck": snapshot.context_kind,
                "imi": included_msg,
                "ifi": included_files,
                "iti": included_tasks,
                "iai": included_artifacts,
                "iki": included_knowledge,
                "sid": snapshot.summary_id,
                "et": snapshot.estimated_tokens,
                "cd": snapshot.context_digest,
                "cp": snapshot.context_preview,
                "ev": snapshot.engine_version,
                "cs": snapshot.call_site,
                "cpk": snapshot.context_profile_key,
                "cpv": snapshot.context_profile_version,
                "cpol": snapshot.context_policy_version,
                "mci": snapshot.model_config_id,
                "mns": snapshot.model_name_snapshot,
                "cwt": snapshot.context_window_tokens,
                "ibt": snapshot.input_budget_tokens,
                "ort": snapshot.output_reserve_tokens,
                "rrt": snapshot.runtime_reserve_tokens,
                "sm": snapshot.safety_margin_tokens,
                "tit": snapshot.target_input_tokens,
                "eit": snapshot.estimated_input_tokens,
                "ait": snapshot.actual_input_tokens,
                "aot": snapshot.actual_output_tokens,
                "ssj": self._serialize_json(snapshot.section_stats_json),
                "irj": self._serialize_json(snapshot.included_refs_json),
                "drj": self._serialize_json(snapshot.dropped_refs_json),
                "rrj": self._serialize_json(snapshot.retrieval_run_ids_json),
                "coj": self._serialize_json(snapshot.compaction_json),
                "toj": self._serialize_json(snapshot.tool_output_json),
                "fbj": self._serialize_json(snapshot.fallback_json),
                "lat": self._serialize_json(snapshot.latency_json),
                "pd": snapshot.prompt_digest,
                "pe": snapshot.prompt_excerpt,
                "fpp": snapshot.full_prompt_payload_id,
                "st": snapshot.status,
                "ec": snapshot.error_code,
                "ca": snapshot.completed_at,
                "ct": snapshot.created_at,
            },
        )
        await self.session.flush()

        id_result = await self.session.execute(text("SELECT LAST_INSERT_ID()"))
        snapshot.id = id_result.scalar()
        return snapshot

    @staticmethod
    def _serialize_json(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return _json.dumps(value, ensure_ascii=False, default=str)

    async def get_by_public_id(
        self, public_id: str, user_id: int
    ) -> ContextSnapshot | None:
        result = await self.session.execute(
            select(ContextSnapshot).where(
                ContextSnapshot.public_id == public_id,
                ContextSnapshot.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()


# 模块定位:ContextSnapshot 仓储(F016 审计 + Usage 数据源)
#
# 链路:
#   ContextSnapshotService.save_snapshot(...)
#   ContextUsageService.get_current_usage → 读最近 kind=active,status=completed
#
# 关键约束:
#   - **不**存完整 prompt(只头部 + IDs);
#   - status='partial' 不参与 active usage 计数(告警路径);
#   - estimated_tokens 用 token_estimator,不入真值计算。
