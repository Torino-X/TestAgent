"""Queries for canonical template assets and public marketplace rows."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.template_asset import TemplateAsset
from app.models.template_version import TemplateVersion
from app.models.user import User
from app.models.user_template import UserTemplate
from app.repositories.base import BaseRepository, ensure_model_id


@dataclass(frozen=True)
class TemplateMarketRow:
    asset: TemplateAsset
    version: TemplateVersion
    owner: User
    is_saved: bool


class TemplateRepository(BaseRepository[TemplateAsset]):
    model = TemplateAsset

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def create(self, asset: TemplateAsset) -> TemplateAsset:
        await ensure_model_id(self.session, TemplateAsset, asset)
        self.session.add(asset)
        await self.session.flush()
        return asset

    async def get_by_id(self, template_id: int) -> TemplateAsset | None:
        return await self.session.get(TemplateAsset, template_id)

    async def get_by_public_id(self, public_id: str) -> TemplateAsset | None:
        result = await self.session.execute(
            select(TemplateAsset).where(
                TemplateAsset.public_id == public_id,
                TemplateAsset.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_public_market_template(self, public_id: str) -> TemplateAsset | None:
        result = await self.session.execute(
            select(TemplateAsset).where(
                TemplateAsset.public_id == public_id,
                TemplateAsset.visibility == "public",
                TemplateAsset.status == "active",
                TemplateAsset.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_active_public_by_name(
        self, *, name: str, category_code: str
    ) -> TemplateAsset | None:
        """Resolve the deployment-selected public template without a volatile ID."""
        result = await self.session.execute(
            select(TemplateAsset)
            .where(
                TemplateAsset.name == name,
                TemplateAsset.category_code == category_code,
                TemplateAsset.visibility == "public",
                TemplateAsset.status == "active",
                TemplateAsset.current_version_id.is_not(None),
                TemplateAsset.deleted_at.is_(None),
            )
            .order_by(TemplateAsset.updated_at.desc(), TemplateAsset.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_market(
        self,
        user_id: int,
        *,
        query: str = "",
        category: str = "all",
        sort: str = "newest",
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[TemplateMarketRow], int]:
        saved = aliased(UserTemplate)
        conditions = [
            TemplateAsset.visibility == "public",
            TemplateAsset.status == "active",
            TemplateAsset.deleted_at.is_(None),
            TemplateVersion.deleted_at.is_(None),
        ]
        if query:
            pattern = f"%{query}%"
            conditions.append(or_(TemplateAsset.name.ilike(pattern), TemplateAsset.description.ilike(pattern)))
        if category != "all":
            conditions.append(TemplateAsset.category_code == category)

        base = (
            select(TemplateAsset, TemplateVersion, User, saved.id.is_not(None))
            .join(TemplateVersion, TemplateVersion.id == TemplateAsset.current_version_id)
            .join(User, User.id == TemplateAsset.owner_user_id)
            .outerjoin(
                saved,
                (saved.template_id == TemplateAsset.id)
                & (saved.user_id == user_id)
                & saved.deleted_at.is_(None),
            )
            .where(*conditions)
        )
        count_stmt = select(func.count(TemplateAsset.id)).select_from(TemplateAsset).join(
            TemplateVersion, TemplateVersion.id == TemplateAsset.current_version_id
        ).where(*conditions)
        order = (
            (TemplateAsset.save_count.desc(), TemplateAsset.updated_at.desc(), TemplateAsset.id.desc())
            if sort == "saves"
            else (TemplateAsset.updated_at.desc(), TemplateAsset.id.desc())
        )
        result = await self.session.execute(base.order_by(*order).offset(offset).limit(limit))
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return [TemplateMarketRow(asset, version, owner, bool(is_saved)) for asset, version, owner, is_saved in result.all()], total

    async def set_publication(self, asset_id: int, *, published: bool, now: object) -> None:
        await self.session.execute(
            update(TemplateAsset)
            .where(TemplateAsset.id == asset_id)
            .values(
                visibility="public" if published else "private",
                status="active" if published else "unpublished",
                published_at=now if published else None,
                updated_at=now,
            )
        )

    async def increment_save_count(self, asset_id: int, now: object) -> None:
        await self.session.execute(
            update(TemplateAsset)
            .where(TemplateAsset.id == asset_id)
            .values(save_count=TemplateAsset.save_count + 1, updated_at=now)
        )

    async def soft_delete(self, asset_id: int, now: object) -> None:
        await self.session.execute(
            update(TemplateAsset)
            .where(TemplateAsset.id == asset_id)
            .values(status="deleted", deleted_at=now, updated_at=now)
        )
