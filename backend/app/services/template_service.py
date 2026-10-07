"""Template creation, publication, removal, and download business rules."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    TemplateFileTooLargeError,
    TemplateInvalidCategoryError,
    TemplateNotOwnedError,
    TemplatePublishedDeleteConflictError,
    TemplateUnsupportedFileTypeError,
    TemplateVersionMissingError,
    ValidationError,
)
from app.models.template_asset import TemplateAsset
from app.models.template_version import TemplateVersion
from app.models.user_template import UserTemplate
from app.repositories.template_repository import TemplateRepository
from app.repositories.template_version_repository import TemplateVersionRepository
from app.repositories.user_template_repository import OwnedTemplateRow, UserTemplateRepository
from app.services.template_cover_service import TemplateCoverService, serialize_template_cover
from app.storage.oss_storage import object_storage
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)

TEMPLATE_CATEGORIES = frozenset({"test_plan", "test_case", "defect_analysis_report", "other"})
TEMPLATE_SOURCE_TYPES = frozenset({"owner", "market_saved"})
TEMPLATE_EXTENSIONS = frozenset({"docx", "xlsx"})
_EXPECTED_ARCHIVE_ENTRY = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml"}
_ALLOWED_MIME_TYPES = {
    "docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/octet-stream",
        "application/zip",
    },
    "xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream",
        "application/zip",
    },
}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def serialize_template_row(row: OwnedTemplateRow) -> dict[str, Any]:
    item, asset, version, owner = row.user_template, row.asset, row.version, row.owner
    return {
        "id": item.public_id,
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
        "visibility": asset.visibility,
        "status": asset.status,
        "source_type": item.source_type,
        "published_at": _iso(asset.published_at),
        "updated_at": _iso(item.updated_at),
        "last_used_at": _iso(item.last_used_at),
        "download_url": f"/api/templates/mine/{item.public_id}/download",
        "preview_url": f"/api/templates/mine/{item.public_id}/preview",
        "cover": serialize_template_cover(
            version,
            pdf_url=f"/api/templates/mine/{item.public_id}/preview/pdf",
        ),
    }


class TemplateService:
    def __init__(self, session: AsyncSession, *, storage=None) -> None:
        self._session = session
        self._storage = storage or object_storage
        self._templates = TemplateRepository(session)
        self._versions = TemplateVersionRepository(session)
        self._user_templates = UserTemplateRepository(session)

    async def create_template(
        self,
        *,
        content: bytes,
        file_name: str,
        user_id: int,
        name: str,
        category_code: str,
        description: str | None,
        tags: list[str],
        publish: bool,
        content_type: str | None,
    ) -> dict[str, Any]:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 255:
            raise ValidationError("模板名称长度必须为 1 到 255 个字符")
        category = self.validate_category(category_code)
        normalized_tags = self.normalize_tags(tags)
        original_filename, extension, mime_type = self.validate_file(
            content, file_name, content_type
        )
        now = utcnow()
        asset = TemplateAsset(
            public_id=generate_public_id("template"),
            owner_user_id=user_id,
            name=normalized_name,
            category_code=category,
            description=(description or "").strip() or None,
            tags_json=normalized_tags,
            visibility="public" if publish else "private",
            status="active",
            save_count=0,
            published_at=now if publish else None,
            created_at=now,
            updated_at=now,
        )
        await self._templates.create(asset)

        stored = None
        cover_storage_path: str | None = None
        version: TemplateVersion | None = None
        try:
            stored = await self._storage.save_template(
                content,
                original_filename,
                str(user_id),
                asset.public_id,
                1,
            )
            version = await self._versions.create(TemplateVersion(
                public_id=generate_public_id("template_version"),
                template_id=asset.id,
                version_no=1,
                original_filename=original_filename,
                file_ext=extension,
                mime_type=mime_type,
                file_size=len(content),
                file_hash=hashlib.sha256(content).hexdigest(),
                storage_path=stored.storage_path,
                created_at=now,
            ))
            cover_storage_path = await TemplateCoverService(
                self._session,
                storage=self._storage,
            ).prepare_uploaded_version(
                version,
                owner_user_id=user_id,
                content=content,
            )
            asset.current_version_id = version.id
            item = await self._user_templates.create(UserTemplate(
                public_id=generate_public_id("user_template"),
                user_id=user_id,
                template_id=asset.id,
                version_id=version.id,
                source_type="owner",
                created_at=now,
                updated_at=now,
            ))
            await self._session.flush()
        except Exception:
            if cover_storage_path is None and version is not None:
                cover_storage_path = version.cover_storage_path
            if cover_storage_path:
                try:
                    await self._storage.delete_file(cover_storage_path)
                except Exception as cleanup_error:
                    logger.warning(
                        "template cover cleanup failed | template=%s | error=%s",
                        asset.public_id,
                        type(cleanup_error).__name__,
                    )
            if stored is not None:
                try:
                    await self._storage.delete_file(stored.storage_path)
                except Exception as cleanup_error:
                    logger.warning(
                        "template canonical cleanup failed | template=%s | error=%s",
                        asset.public_id,
                        type(cleanup_error).__name__,
                    )
            raise

        owner_row = await self._user_templates.get_owned(item.public_id, user_id)
        if owner_row is None:
            raise TemplateNotOwnedError()
        logger.info(
            "template.uploaded | template=%s | user_id=%s | category=%s | file_ext=%s | status=%s",
            asset.public_id,
            user_id,
            category,
            extension,
            asset.status,
        )
        return serialize_template_row(owner_row)

    async def publish(self, user_template_public_id: str, user_id: int) -> dict[str, Any]:
        row = await self._require_owner_item(user_template_public_id, user_id)
        if row.asset.visibility != "public" or row.asset.status != "active":
            await self._templates.set_publication(row.asset.id, published=True, now=utcnow())
            await self._session.flush()
            row.asset.visibility = "public"
            row.asset.status = "active"
        logger.info("template.published | template=%s | user_id=%s", row.asset.public_id, user_id)
        return serialize_template_row(row)

    async def unpublish(self, user_template_public_id: str, user_id: int) -> dict[str, Any]:
        row = await self._require_owner_item(user_template_public_id, user_id)
        if row.asset.visibility != "private" or row.asset.status != "unpublished":
            await self._templates.set_publication(row.asset.id, published=False, now=utcnow())
            await self._session.flush()
            row.asset.visibility = "private"
            row.asset.status = "unpublished"
        logger.info("template.unpublished | template=%s | user_id=%s", row.asset.public_id, user_id)
        return serialize_template_row(row)

    async def remove(self, user_template_public_id: str, user_id: int) -> None:
        row = await self._user_templates.get_owned(user_template_public_id, user_id)
        if row is None:
            raise TemplateNotOwnedError()
        now = utcnow()
        if row.user_template.source_type == "owner":
            if row.asset.visibility == "public" and row.asset.status == "active":
                raise TemplatePublishedDeleteConflictError()
            await self._templates.soft_delete(row.asset.id, now)
        await self._user_templates.soft_delete(row.user_template.id, now)
        logger.info(
            "template.removed | template=%s | user_id=%s | source=%s",
            row.asset.public_id,
            user_id,
            row.user_template.source_type,
        )

    async def get_download_stream(self, user_template_public_id: str, user_id: int):
        row = await self._user_templates.get_owned(user_template_public_id, user_id)
        if row is None:
            raise TemplateNotOwnedError()
        version = await self._versions.get_active(row.user_template.version_id)
        if version is None:
            raise TemplateVersionMissingError()
        try:
            if not await self._storage.exists(version.storage_path):
                raise FileNotFoundError(version.storage_path)
            stream = self._storage.open_file(version.storage_path)
        except FileNotFoundError as exc:
            raise TemplateVersionMissingError() from exc
        logger.info(
            "template.downloaded | template=%s | user_id=%s | file_ext=%s",
            row.asset.public_id,
            user_id,
            version.file_ext,
        )
        return stream, version.original_filename, version.mime_type or "application/octet-stream"

    async def _require_owner_item(self, public_id: str, user_id: int) -> OwnedTemplateRow:
        row = await self._user_templates.get_owned(public_id, user_id)
        if row is None or row.user_template.source_type != "owner":
            raise TemplateNotOwnedError()
        return row

    @staticmethod
    def validate_category(category_code: str) -> str:
        normalized = category_code.strip().lower()
        if normalized not in TEMPLATE_CATEGORIES:
            raise TemplateInvalidCategoryError()
        return normalized

    @staticmethod
    def normalize_tags(tags: list[str]) -> list[str]:
        result: list[str] = []
        for value in tags:
            tag = str(value).strip()
            if tag and tag not in result:
                result.append(tag[:32])
            if len(result) == 8:
                break
        return result

    @staticmethod
    def validate_file(content: bytes, file_name: str, content_type: str | None) -> tuple[str, str, str]:
        if not content:
            raise ValidationError("模板文件不能为空")
        if len(content) > get_settings().upload_max_bytes:
            raise TemplateFileTooLargeError()
        cleaned = re.sub(r"[\x00-\x1f\x7f]", "", Path(file_name.replace("\\", "/")).name).strip()
        if not cleaned:
            raise ValidationError("模板文件名不能为空")
        cleaned = cleaned[:255]
        extension = Path(cleaned).suffix.lower().lstrip(".")
        if extension not in TEMPLATE_EXTENSIONS:
            raise TemplateUnsupportedFileTypeError()
        normalized_mime = (content_type or "").split(";", 1)[0].strip().lower()
        if normalized_mime and normalized_mime not in _ALLOWED_MIME_TYPES[extension]:
            raise TemplateUnsupportedFileTypeError()
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                names = {name.casefold() for name in archive.namelist()}
                if _EXPECTED_ARCHIVE_ENTRY[extension].casefold() not in names:
                    raise TemplateUnsupportedFileTypeError()
                if any(name.endswith("vbaproject.bin") for name in names):
                    raise TemplateUnsupportedFileTypeError()
                bad_member = archive.testzip()
                if bad_member is not None:
                    raise TemplateUnsupportedFileTypeError()
        except (zipfile.BadZipFile, OSError) as exc:
            raise TemplateUnsupportedFileTypeError() from exc
        canonical_mime = mimetypes.guess_type(cleaned)[0] or next(iter(_ALLOWED_MIME_TYPES[extension]))
        return cleaned, extension, canonical_mime
