"""Owner-scoped Project repository."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project
from app.repositories.base import BaseRepository, ensure_model_id


class ProjectRepository(BaseRepository[Project]):
    model = Project

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def create(self, project: Project) -> Project:
        await ensure_model_id(self.session, Project, project)
        self.session.add(project)
        await self.session.flush()
        return project

    async def get_owned(self, public_id: str, user_id: int) -> Project | None:
        result = await self.session.execute(
            select(Project).where(
                Project.public_id == public_id,
                Project.user_id == user_id,
                Project.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_owned(
        self,
        user_id: int,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 20,
    ) -> list[Project]:
        stmt = select(Project).where(
            Project.user_id == user_id,
            Project.deleted_at.is_(None),
        )
        if query:
            stmt = stmt.where(Project.name.ilike(f"%{query}%"))
        result = await self.session.execute(
            stmt.order_by(
                Project.pinned_at.is_(None).asc(),
                Project.pinned_at.desc(),
                Project.updated_at.desc(),
                Project.id.desc(),
            ).offset(offset).limit(limit)
        )
        return list(result.scalars().all())

    async def count_owned(self, user_id: int, *, query: str = "") -> int:
        stmt = select(func.count(Project.id)).where(
            Project.user_id == user_id,
            Project.deleted_at.is_(None),
        )
        if query:
            stmt = stmt.where(Project.name.ilike(f"%{query}%"))
        return int((await self.session.execute(stmt)).scalar_one())

    async def touch(self, project_id: int, now: object) -> None:
        await self.session.execute(
            update(Project).where(Project.id == project_id).values(last_activity_at=now, updated_at=now)
        )
