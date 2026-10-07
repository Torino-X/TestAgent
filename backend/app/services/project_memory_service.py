"""Project-scoped durable memory writers with provenance and idempotency."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.context_engine.memory.memory_service import MemoryService
from app.repositories.context_engine_repositories import ContextMemoryRepository
from app.repositories.project_repository import ProjectRepository


class ProjectMemoryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._memories = ContextMemoryRepository(session)
        self._projects = ProjectRepository(session)

    async def record_task_completion(
        self,
        *,
        user_id: int,
        project_public_id: str,
        task_public_id: str,
        summary: str | None,
        artifact: dict[str, Any] | None = None,
        confirmed_sections: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        project = await self._projects.get_owned(project_public_id, user_id)
        if project is None or project.memory_mode == "off":
            return {"created": False, "reason": "project_memory_disabled"}
        dedupe_key = f"project_progress:{task_public_id}"
        existing = await self._memories.find_conflict(
            user_id,
            "workspace",
            dedupe_key,
            workspace_key=project.context_workspace_key,
        )
        if existing is not None:
            return {"created": False, "memory_public_id": existing.public_id}

        content_parts = [str(summary or "任务已完成").strip()[:1200]]
        artifact = artifact if isinstance(artifact, dict) else {}
        artifact_name = artifact.get("file_name") or artifact.get("name")
        artifact_id = artifact.get("public_id") or artifact.get("artifact_id")
        if artifact_name or artifact_id:
            content_parts.append(
                "生成产物：" + " / ".join(str(value) for value in (artifact_name, artifact_id) if value)
            )
        section_titles = [
            str(item.get("title") or item.get("name") or "").strip()
            for item in (confirmed_sections or [])
            if isinstance(item, dict)
        ]
        section_titles = [item for item in section_titles if item][:20]
        if section_titles:
            content_parts.append("已确认章节：" + "、".join(section_titles))
        service = MemoryService(self._session)
        result = await service.create_candidate(
            user_id=user_id,
            scope_type="workspace",
            workspace_key=project.context_workspace_key,
            agent_type="test_plan_generation",
            memory_type="progress",
            title=f"任务进度：{task_public_id}",
            content="\n".join(content_parts),
            dedupe_key=dedupe_key,
            evidence=[{
                "source_type": "agent_task",
                "source_public_id": task_public_id,
                "relation_type": "derived_from",
                "evidence_excerpt": str(summary or "任务已完成")[:500],
            }],
        )
        await service.activate(
            user_id=user_id,
            memory_public_id=result["memory_public_id"],
        )
        self._register_cache_invalidation(user_id, project_public_id)
        return {"created": True, **result, "status": "active"}

    async def record_chat_facts(
        self,
        *,
        user_id: int,
        project_public_id: str,
        conversation_public_id: str,
        facts: list[dict[str, str]],
    ) -> dict[str, Any]:
        project = await self._projects.get_owned(project_public_id, user_id)
        if project is None or project.memory_mode == "off":
            return {"persisted": 0}
        persisted = 0
        service = MemoryService(self._session)
        for item in list(facts or [])[:5]:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            stable_key = str(item.get("key") or "").strip()
            if not stable_key:
                stable_key = hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()[:32]
            dedupe_key = f"project_fact:{stable_key}"
            existing = await self._memories.find_conflict(
                user_id,
                "workspace",
                dedupe_key,
                workspace_key=project.context_workspace_key,
            )
            if existing is not None and existing.content == content:
                continue
            result = await service.create_candidate(
                user_id=user_id,
                scope_type="workspace",
                workspace_key=project.context_workspace_key,
                agent_type=None,
                memory_type="decision",
                title=content[:80],
                content=content[:2000],
                dedupe_key=dedupe_key,
                evidence=[{
                    "source_type": "conversation",
                    "source_public_id": conversation_public_id,
                    "relation_type": "extracted_from",
                    "evidence_excerpt": content[:500],
                }],
            )
            await service.activate(user_id=user_id, memory_public_id=result["memory_public_id"])
            if existing is not None:
                existing.status = "archived"
                existing.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
                await self._session.flush()
            persisted += 1
        if persisted:
            self._register_cache_invalidation(user_id, project_public_id)
        return {"persisted": persisted}

    def _register_cache_invalidation(self, user_id: int, project_public_id: str) -> None:
        from app.cache.domains.project_cache import get_project_cache
        from app.db.sync import register_after_commit

        async def invalidate(_session):
            await get_project_cache().invalidate(user_id, project_public_id)

        register_after_commit(self._session, invalidate)


__all__ = ["ProjectMemoryService"]
