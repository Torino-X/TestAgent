"""Owner-scoped queries for entries in My Templates."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.template_asset import TemplateAsset
from app.models.template_version import TemplateVersion
from app.models.user import User
from app.models.user_template import UserTemplate
from app.repositories.base import BaseRepository, ensure_model_id


@dataclass(frozen=True)
class OwnedTemplateRow:
    user_template: UserTemplate
    asset: TemplateAsset
    version: TemplateVersion
    owner: User


class UserTemplateRepository(BaseRepository[UserTemplate]):
    model = UserTemplate

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def create(self, item: UserTemplate) -> UserTemplate:
        await ensure_model_id(self.session, UserTemplate, item)
        self.session.add(item)
        await self.session.flush()
        return item

    async def get_owned(self, public_id: str, user_id: int) -> OwnedTemplateRow | None:
        result = await self.session.execute(
            select(UserTemplate, TemplateAsset, TemplateVersion, User)
            .join(TemplateAsset, TemplateAsset.id == UserTemplate.template_id)
            .join(TemplateVersion, TemplateVersion.id == UserTemplate.version_id)
            .join(User, User.id == TemplateAsset.owner_user_id)
            .where(
                UserTemplate.public_id == public_id,
                UserTemplate.user_id == user_id,
                UserTemplate.deleted_at.is_(None),
                TemplateAsset.deleted_at.is_(None),
                TemplateVersion.deleted_at.is_(None),
            )
        )
        row = result.one_or_none()
        return OwnedTemplateRow(*row) if row else None

    async def get_by_user_and_template(
        self, user_id: int, template_id: int, *, include_deleted: bool = False
    ) -> UserTemplate | None:
        stmt = select(UserTemplate).where(
            UserTemplate.user_id == user_id,
            UserTemplate.template_id == template_id,
        )
        if not include_deleted:
            stmt = stmt.where(UserTemplate.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_owned(
        self,
        user_id: int,
        *,
        query: str = "",
        category: str = "all",
        source_type: str = "all",
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[OwnedTemplateRow], int]:
        conditions = [
            UserTemplate.user_id == user_id,
            UserTemplate.deleted_at.is_(None),
            TemplateAsset.deleted_at.is_(None),
            TemplateVersion.deleted_at.is_(None),
        ]
        if query:
            pattern = f"%{query}%"
            conditions.append(or_(TemplateAsset.name.ilike(pattern), TemplateAsset.description.ilike(pattern)))
        if category != "all":
            conditions.append(TemplateAsset.category_code == category)
        if source_type != "all":
            conditions.append(UserTemplate.source_type == source_type)
        joins = (
            select(UserTemplate, TemplateAsset, TemplateVersion, User)
            .join(TemplateAsset, TemplateAsset.id == UserTemplate.template_id)
            .join(TemplateVersion, TemplateVersion.id == UserTemplate.version_id)
            .join(User, User.id == TemplateAsset.owner_user_id)
            .where(*conditions)
        )
        count_stmt = select(func.count(UserTemplate.id)).select_from(UserTemplate).join(
            TemplateAsset, TemplateAsset.id == UserTemplate.template_id
        ).join(TemplateVersion, TemplateVersion.id == UserTemplate.version_id).where(*conditions)
        result = await self.session.execute(
            joins.order_by(UserTemplate.updated_at.desc(), UserTemplate.id.desc()).offset(offset).limit(limit)
        )
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return [OwnedTemplateRow(*row) for row in result.all()], total

    async def restore(self, item_id: int, version_id: int, now: object) -> None:
        await self.session.execute(
            update(UserTemplate)
            .where(UserTemplate.id == item_id)
            .values(deleted_at=None, version_id=version_id, updated_at=now)
        )

    async def soft_delete(self, item_id: int, now: object) -> None:
        await self.session.execute(
            update(UserTemplate).where(UserTemplate.id == item_id).values(deleted_at=now, updated_at=now)
        )

    async def touch_last_used(self, item_id: int, now: object) -> None:
        await self.session.execute(
            update(UserTemplate).where(UserTemplate.id == item_id).values(last_used_at=now, updated_at=now)
        )
