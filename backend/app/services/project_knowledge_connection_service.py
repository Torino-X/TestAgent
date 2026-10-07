"""Project knowledge-connection business logic."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import ProjectKnowledgeBinding
from app.repositories.project_knowledge_binding_repository import ProjectKnowledgeBindingRepository
from app.repositories.project_repository import ProjectRepository
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id


class ProjectKnowledgeConnectionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._projects = ProjectRepository(session)
        self._bindings = ProjectKnowledgeBindingRepository(session)

    async def list_ids(self, project_public_id: str, user_id: int) -> list[str]:
        project = await self._require_project(project_public_id, user_id)
        return [row.knowledge_id for row in await self._bindings.list_enabled(project.id)]

    async def replace(self, project_public_id: str, user_id: int, knowledge_ids: list[str]) -> list[str]:
        project = await self._require_project(project_public_id, user_id)
        normalized = list(dict.fromkeys(value.strip() for value in knowledge_ids if value.strip()))
        existing = {row.knowledge_id: row for row in await self._bindings.list_all(project.id)}
        now = utcnow()
        selected = set(normalized)
        for knowledge_id, row in existing.items():
            row.enabled = knowledge_id in selected
            row.deleted_at = None if row.enabled else now
            row.updated_at = now
        for priority, knowledge_id in enumerate(normalized):
            row = existing.get(knowledge_id)
            if row is not None:
                row.priority = priority
                continue
            await self._bindings.create(ProjectKnowledgeBinding(
                public_id=generate_public_id("project_knowledge_binding"),
                project_id=project.id,
                knowledge_id=knowledge_id,
                enabled=True,
                priority=priority,
                created_at=now,
                updated_at=now,
            ))
        await self._projects.touch(project.id, now)
        await self._register_cache_invalidation(user_id, project.public_id)
        return normalized

    async def _require_project(self, public_id: str, user_id: int):
        project = await self._projects.get_owned(public_id, user_id)
        if project is None:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("项目")
        return project

    async def _register_cache_invalidation(self, user_id: int, project_public_id: str) -> None:
        from app.cache.domains.project_cache import get_project_cache
        from app.db.sync import register_after_commit

        async def invalidate(_session):
            await get_project_cache().invalidate(user_id, project_public_id)

        register_after_commit(self._session, invalidate)
