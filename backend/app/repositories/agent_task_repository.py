"""Persistence boundary for Agent Tasks.

New rows are LangGraph-only.  ``engine_type`` is deliberately absent from every
update method so a historical row cannot be converted into an executable task.
"""

from __future__ import annotations

import json

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_task import AgentTask
from app.repositories.base import BaseRepository


class AgentTaskRepository(BaseRepository[AgentTask]):
    model = AgentTask

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_public_id(self, public_id: str) -> AgentTask | None:
        result = await self.session.execute(
            select(AgentTask).where(
                AgentTask.public_id == public_id,
                AgentTask.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_owned_task(self, public_id: str, user_id: int) -> AgentTask | None:
        """Return a task only when both public id and owner match."""
        result = await self.session.execute(
            select(AgentTask).where(
                AgentTask.public_id == public_id,
                AgentTask.user_id == user_id,
                AgentTask.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_by_conversation(
        self, user_id: int, conv_internal_id: int
    ) -> list[AgentTask]:
        result = await self.session.execute(
            select(AgentTask)
            .where(
                AgentTask.user_id == user_id,
                AgentTask.conversation_id == conv_internal_id,
                AgentTask.deleted_at.is_(None),
            )
            .order_by(AgentTask.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_latest_by_conversation(
        self, user_id: int, conv_internal_id: int
    ) -> AgentTask | None:
        result = await self.session.execute(
            select(AgentTask)
            .where(
                AgentTask.user_id == user_id,
                AgentTask.conversation_id == conv_internal_id,
                AgentTask.deleted_at.is_(None),
            )
            .order_by(AgentTask.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def create(self, task: AgentTask) -> AgentTask:
        """Insert a new LangGraph task and populate its database id."""
        engine_type = str(task.engine_type or "").strip().lower()
        if engine_type != "langgraph":
            raise ValueError("new AgentTask rows require engine_type='langgraph'")

        result = await self.session.execute(
            text(
                "INSERT INTO agent_tasks "
                "(public_id, user_id, conversation_id, project_id, task_type, status, title, "
                " user_instruction, requirement_file_id, template_file_id, "
                " trigger_message_id, plan_json, task_context_json, review_result_json, "
                " error_code, error_message, started_at, completed_at, created_at, updated_at, "
                " context_workspace_key, context_engine_version, engine_type, graph_name, "
                " graph_version, thread_id, active_run_id, runtime_status) "
                "VALUES "
                "(:pid, :uid, :cid, :prid, :tt, :st, :title, :ui, :rf, :tf, :tmi, "
                " :pj, :cj, :rj, :ec, :em, :sa, :ca, :cr, :up, :cwk, :cev, "
                " :et, :gn, :gv, :th, :ar, :rs)"
            ),
            {
                "pid": task.public_id,
                "uid": task.user_id,
                "cid": task.conversation_id,
                "prid": getattr(task, "project_id", None),
                "tt": task.task_type,
                "st": task.status,
                "title": task.title,
                "ui": task.user_instruction,
                "rf": task.requirement_file_id,
                "tf": task.template_file_id,
                "tmi": task.trigger_message_id,
                "pj": self._serialize_json(task.plan_json),
                "cj": self._serialize_json(task.task_context_json),
                "rj": self._serialize_json(task.review_result_json),
                "ec": task.error_code,
                "em": task.error_message,
                "sa": task.started_at,
                "ca": task.completed_at,
                "cr": task.created_at,
                "up": task.updated_at,
                "cwk": getattr(task, "context_workspace_key", None),
                "cev": getattr(task, "context_engine_version", None),
                "et": engine_type,
                "gn": task.graph_name,
                "gv": task.graph_version,
                "th": task.thread_id,
                "ar": task.active_run_id,
                "rs": task.runtime_status,
            },
        )
        await self.session.flush()
        id_result = await self.session.execute(text("SELECT LAST_INSERT_ID()"))
        task.id = id_result.scalar()
        return task

    @staticmethod
    def _serialize_json(value: dict | None) -> str | None:
        if value is None or isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str)

    async def update_status(self, task_id: int, status: str) -> None:
        """Update status only; the persisted engine identity is immutable."""
        await self.session.execute(
            text("UPDATE agent_tasks SET status = :st WHERE id = :tid"),
            {"st": status, "tid": task_id},
        )

    async def update_context_json(
        self,
        task_id: int,
        context_json: str,
        *,
        allow_missing_manifest_from_patch: bool = False,
    ) -> None:
        """Deep-merge business context while preserving the frozen manifest.

        Normal business updates cannot modify the reserved manifest.  The task
        creation flow is the only exception: it may restore its just-created,
        already-validated manifest when a database row unexpectedly lacks it.
        This closes the gap between the task INSERT and its later context
        enrichment update without allowing arbitrary callers to replace a
        frozen decision.
        """
        from app.context_engine.freeze.service import (
            RESERVED_KEY,
            extract_manifest,
            merge_context_json,
            write_once_validate,
        )

        existing_row = await self.session.execute(
            select(AgentTask.task_context_json).where(AgentTask.id == task_id)
        )
        existing = existing_row.scalar_one_or_none()
        existing_dict = existing if isinstance(existing, dict) else {}
        try:
            patch = json.loads(context_json) if isinstance(context_json, str) else context_json
        except (TypeError, ValueError):
            patch = {}
        merged = merge_context_json(existing_dict, patch)
        if allow_missing_manifest_from_patch and extract_manifest(existing_dict) is None:
            candidate_manifest = extract_manifest(patch)
            if candidate_manifest is not None:
                write_once_validate(candidate_manifest)
                merged[RESERVED_KEY] = candidate_manifest
        if RESERVED_KEY in merged and merged[RESERVED_KEY] is None:
            merged.pop(RESERVED_KEY, None)
        await self.session.execute(
            update(AgentTask)
            .where(AgentTask.id == task_id)
            .values(task_context_json=merged)
        )

    async def write_frozen_manifest(self, task_id: int, context_json: dict) -> int:
        """Write the frozen feature manifest once using a database CAS."""
        from app.context_engine.freeze.service import RESERVED_KEY

        result = await self.session.execute(
            text(
                "UPDATE agent_tasks SET task_context_json = :ctx "
                "WHERE id = :tid AND JSON_EXTRACT(task_context_json, :reserved) IS NULL"
            ),
            {
                "ctx": json.dumps(context_json, ensure_ascii=False),
                "tid": task_id,
                "reserved": f"$.{RESERVED_KEY}",
            },
        )
        return int(result.rowcount or 0)

    async def update_plan(self, task_id: int, plan: dict) -> None:
        await self.session.execute(
            update(AgentTask).where(AgentTask.id == task_id).values(plan_json=plan)
        )

    async def update_snapshot_fields(
        self,
        task_id: int,
        snapshot_id: int,
        expected_version: int,
        current_node: str,
        resume_node: str | None,
        now,
        status: str | None = None,
    ) -> int:
        """CAS-update checkpoint fields and optionally the task status."""
        status_clause = ", status = :status" if status else ""
        params = {
            "sid": snapshot_id,
            "cn": current_node,
            "rn": resume_node,
            "now": now,
            "tid": task_id,
            "ev": expected_version,
        }
        if status:
            params["status"] = status
        result = await self.session.execute(
            text(
                "UPDATE agent_tasks "
                "SET latest_snapshot_id = :sid, context_version = context_version + 1, "
                "current_node = :cn, resume_node = :rn, checkpoint_updated_at = :now"
                f"{status_clause} WHERE id = :tid AND context_version = :ev"
            ),
            params,
        )
        await self.session.flush()
        return result.rowcount

    async def update_engine_fields(
        self,
        task_id: int,
        *,
        graph_name: str | None = None,
        graph_version: str | None = None,
        thread_id: str | None = None,
        active_run_id: str | None = None,
        runtime_status: str | None = None,
    ) -> None:
        """Update graph runtime metadata without exposing ``engine_type``."""
        values: dict[str, object] = {}
        for name, value in (
            ("graph_name", graph_name),
            ("graph_version", graph_version),
            ("thread_id", thread_id),
            ("active_run_id", active_run_id),
            ("runtime_status", runtime_status),
        ):
            if value is not None:
                values[name] = value
        if not values:
            return
        set_clause = ", ".join(f"{column} = :{column}" for column in values)
        await self.session.execute(
            text(f"UPDATE agent_tasks SET {set_clause} WHERE id = :tid"),
            {**values, "tid": task_id},
        )
