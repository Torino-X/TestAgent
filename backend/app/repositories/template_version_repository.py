"""Repository for immutable template versions."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.template_version import TemplateVersion
from app.repositories.base import BaseRepository, ensure_model_id


class TemplateVersionRepository(BaseRepository[TemplateVersion]):
    model = TemplateVersion

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def create(self, version: TemplateVersion) -> TemplateVersion:
        await ensure_model_id(self.session, TemplateVersion, version)
        self.session.add(version)
        await self.session.flush()
        return version

    async def get_active(self, version_id: int) -> TemplateVersion | None:
        result = await self.session.execute(
            select(TemplateVersion).where(
                TemplateVersion.id == version_id,
                TemplateVersion.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()
