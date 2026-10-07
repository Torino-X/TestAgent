"""Project-to-library-file relation repository."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import ProjectSource
from app.models.uploaded_file import UploadedFile
from app.repositories.base import BaseRepository, ensure_model_id


class ProjectSourceRepository(BaseRepository[ProjectSource]):
    model = ProjectSource

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def create(self, source: ProjectSource) -> ProjectSource:
        await ensure_model_id(self.session, ProjectSource, source)
        self.session.add(source)
        await self.session.flush()
        return source

    async def get_any_by_project_file(self, project_id: int, uploaded_file_id: int) -> ProjectSource | None:
        result = await self.session.execute(
            select(ProjectSource).where(
                ProjectSource.project_id == project_id,
                ProjectSource.uploaded_file_id == uploaded_file_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_active(self, project_id: int, public_id: str) -> ProjectSource | None:
        result = await self.session.execute(
            select(ProjectSource).where(
                ProjectSource.project_id == project_id,
                ProjectSource.public_id == public_id,
                ProjectSource.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_with_files(self, project_id: int) -> list[tuple[ProjectSource, UploadedFile]]:
        result = await self.session.execute(
            select(ProjectSource, UploadedFile)
            .join(UploadedFile, UploadedFile.id == ProjectSource.uploaded_file_id)
            .where(
                ProjectSource.project_id == project_id,
                ProjectSource.deleted_at.is_(None),
                ProjectSource.status == "active",
                ProjectSource.is_current.is_(True),
                UploadedFile.deleted_at.is_(None),
            )
            .order_by(ProjectSource.sort_order.asc(), ProjectSource.updated_at.desc())
        )
        return list(result.all())

    async def soft_delete_all(self, project_id: int, now: object) -> None:
        await self.session.execute(
            update(ProjectSource)
            .where(ProjectSource.project_id == project_id, ProjectSource.deleted_at.is_(None))
            .values(status="removed", is_current=False, deleted_at=now, updated_at=now)
        )
