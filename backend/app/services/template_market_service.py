"""Marketplace discovery, My Templates listing, and idempotent saves."""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import TemplateNotFoundError, ValidationError
from app.models.user_template import UserTemplate
from app.repositories.template_repository import TemplateMarketRow, TemplateRepository
from app.repositories.user_template_repository import UserTemplateRepository
from app.services.template_service import (
    TEMPLATE_SOURCE_TYPES,
    TemplateService,
    _iso,
    serialize_template_row,
)
from app.services.template_cover_service import serialize_template_cover
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id


class TemplateMarketService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._templates = TemplateRepository(session)
        self._user_templates = UserTemplateRepository(session)

    async def list_market(
        self,
        user_id: int,
        *,
        query: str = "",
        category: str = "all",
        sort: str = "newest",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        normalized_category = self._category_filter(category)
        if sort not in {"newest", "saves"}:
            raise ValidationError("模板排序参数不合法")
        rows, total = await self._templates.list_market(
            user_id,
            query=query.strip(),
            category=normalized_category,
            sort=sort,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return self._page([self._serialize_market(row) for row in rows], total, page, page_size)

    async def list_mine(
        self,
        user_id: int,
        *,
        query: str = "",
        category: str = "all",
        source_type: str = "all",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        normalized_category = self._category_filter(category)
        if source_type != "all" and source_type not in TEMPLATE_SOURCE_TYPES:
            raise ValidationError("模板来源参数不合法")
        rows, total = await self._user_templates.list_owned(
            user_id,
            query=query.strip(),
            category=normalized_category,
            source_type=source_type,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return self._page([serialize_template_row(row) for row in rows], total, page, page_size)

    async def save(self, template_public_id: str, user_id: int) -> dict[str, Any]:
        asset = await self._templates.get_public_market_template(template_public_id)
        if asset is None or asset.current_version_id is None:
            raise TemplateNotFoundError()
        now = utcnow()
        existing = await self._user_templates.get_by_user_and_template(
            user_id, asset.id, include_deleted=True
        )
        already_saved = existing is not None and existing.deleted_at is None
        created = False
        item = existing
        if item is None:
            try:
                async with self._session.begin_nested():
                    item = await self._user_templates.create(UserTemplate(
                        public_id=generate_public_id("user_template"),
                        user_id=user_id,
                        template_id=asset.id,
                        version_id=asset.current_version_id,
                        source_type="market_saved",
                        created_at=now,
                        updated_at=now,
                    ))
                created = True
            except IntegrityError:
                item = await self._user_templates.get_by_user_and_template(
                    user_id, asset.id, include_deleted=True
                )
                already_saved = item is not None and item.deleted_at is None
        if item is None:
            raise TemplateNotFoundError()
        if item.deleted_at is not None:
            await self._user_templates.restore(item.id, asset.current_version_id, now)
            item.deleted_at = None
            item.version_id = asset.current_version_id
            item.updated_at = now
        if created:
            await self._templates.increment_save_count(asset.id, now)
        await self._session.flush()
        row = await self._user_templates.get_owned(item.public_id, user_id)
        if row is None:
            raise TemplateNotFoundError()
        return {
            "success": True,
            "already_saved": already_saved,
            "user_template": serialize_template_row(row),
        }

    @staticmethod
    def _serialize_market(row: TemplateMarketRow) -> dict[str, Any]:
        asset, version, owner = row.asset, row.version, row.owner
        return {
            "id": asset.public_id,
            "template_id": asset.public_id,
            "name": asset.name,
            "category_code": asset.category_code,
            "description": asset.description,
            "tags": list(asset.tags_json or []),
            "file_ext": version.file_ext,
            "file_size": version.file_size,
            "version_no": version.version_no,
            "author": {
                "id": owner.public_id,
                "display_name": owner.display_name or owner.username,
            },
            "save_count": asset.save_count,
            "is_saved": row.is_saved,
            "published_at": _iso(asset.published_at),
            "updated_at": _iso(asset.updated_at),
            "preview_url": f"/api/templates/market/{asset.public_id}/preview",
            "cover": serialize_template_cover(
                version,
                pdf_url=f"/api/templates/market/{asset.public_id}/preview/pdf",
            ),
        }

    @staticmethod
    def _category_filter(category: str) -> str:
        normalized = category.strip().lower() or "all"
        if normalized == "all":
            return normalized
        return TemplateService.validate_category(normalized)

    @staticmethod
    def _page(items: list[dict[str, Any]], total: int, page: int, page_size: int) -> dict[str, Any]:
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": page * page_size < total,
        }
