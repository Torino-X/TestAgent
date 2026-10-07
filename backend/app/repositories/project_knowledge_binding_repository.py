"""Project knowledge binding repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import ProjectKnowledgeBinding
from app.repositories.base import BaseRepository, ensure_model_id


class ProjectKnowledgeBindingRepository(BaseRepository[ProjectKnowledgeBinding]):
    model = ProjectKnowledgeBinding

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_enabled(self, project_id: int) -> list[ProjectKnowledgeBinding]:
        result = await self.session.execute(
            select(ProjectKnowledgeBinding).where(
                ProjectKnowledgeBinding.project_id == project_id,
                ProjectKnowledgeBinding.enabled.is_(True),
                ProjectKnowledgeBinding.deleted_at.is_(None),
            ).order_by(ProjectKnowledgeBinding.priority.asc(), ProjectKnowledgeBinding.id.asc())
        )
        return list(result.scalars().all())

    async def list_all(self, project_id: int) -> list[ProjectKnowledgeBinding]:
        result = await self.session.execute(
            select(ProjectKnowledgeBinding).where(ProjectKnowledgeBinding.project_id == project_id)
        )
        return list(result.scalars().all())

    async def create(self, binding: ProjectKnowledgeBinding) -> ProjectKnowledgeBinding:
        await ensure_model_id(self.session, ProjectKnowledgeBinding, binding)
        self.session.add(binding)
        await self.session.flush()
        return binding
