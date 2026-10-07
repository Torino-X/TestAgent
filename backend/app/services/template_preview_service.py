"""Read-only preview adapter for public marketplace templates."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import TemplateNotFoundError, TemplateNotOwnedError, TemplateNotPublicError, TemplateVersionMissingError
from app.repositories.template_repository import TemplateRepository
from app.repositories.template_version_repository import TemplateVersionRepository
from app.repositories.user_template_repository import UserTemplateRepository
from app.schemas.library import LibraryItem, LibraryPreviewResponse
from app.schemas.library import LibrarySpreadsheetPreview
from app.storage.oss_storage import object_storage


class TemplatePreviewService:
    def __init__(self, session: AsyncSession, *, storage=None) -> None:
        self._templates = TemplateRepository(session)
        self._versions = TemplateVersionRepository(session)
        self._user_templates = UserTemplateRepository(session)
        self._storage = storage or object_storage

    async def get_preview(self, template_public_id: str) -> dict:
        asset, version = await self._get_public_version(template_public_id)
        return await self._preview_payload(
            asset,
            version,
            pdf_url=f"/api/templates/market/{asset.public_id}/preview/pdf",
        )

    async def get_user_preview(self, user_template_public_id: str, user_id: int) -> dict:
        row = await self._user_templates.get_owned(user_template_public_id, user_id)
        if row is None:
            raise TemplateNotOwnedError()
        return await self._preview_payload(
            row.asset,
            row.version,
            pdf_url=f"/api/templates/mine/{row.user_template.public_id}/preview/pdf",
        )

    async def _preview_payload(self, asset, version, *, pdf_url: str) -> dict:
        extension = f".{version.file_ext.lower().lstrip('.')}"
        item = LibraryItem(
            id=asset.public_id,
            name=version.original_filename,
            kind="file",
            source="template",
            mime_type=version.mime_type,
            extension=extension,
            size_bytes=version.file_size,
            modified_at=asset.updated_at,
            download_url=None,
        )
        if extension == ".docx":
            preview_status = "queued" if version.cover_status == "missing" else version.cover_status
            return LibraryPreviewResponse(
                item=item,
                viewer="word",
                pdf_url=pdf_url if version.cover_status == "ready" and version.cover_storage_path else None,
                preview_status=preview_status,
                preview_error=version.cover_error,
            ).model_dump(mode="json")
        if extension == ".xlsx":
            payload = version.cover_payload_json or {
                "sheet_name": "Sheet1",
                "columns": [],
                "rows": [],
                "truncated": False,
            }
            preview_status = "queued" if version.cover_status == "missing" else version.cover_status
            return LibraryPreviewResponse(
                item=item,
                viewer="spreadsheet",
                spreadsheet=LibrarySpreadsheetPreview.model_validate(payload),
                preview_status=preview_status,
                preview_error=version.cover_error,
            ).model_dump(mode="json")
        return LibraryPreviewResponse(item=item, viewer="unsupported").model_dump(mode="json")

    async def get_pdf_stream(self, template_public_id: str):
        _asset, version = await self._get_public_version(template_public_id)
        return await self._pdf_stream(version)

    async def get_user_pdf_stream(self, user_template_public_id: str, user_id: int):
        row = await self._user_templates.get_owned(user_template_public_id, user_id)
        if row is None:
            raise TemplateNotOwnedError()
        return await self._pdf_stream(row.version)

    async def _pdf_stream(self, version):
        if (
            version.file_ext.lower().lstrip(".") != "docx"
            or version.cover_status != "ready"
            or not version.cover_storage_path
        ):
            raise TemplateVersionMissingError()
        if not await self._storage.exists(version.cover_storage_path):
            raise TemplateVersionMissingError()
        return self._storage.open_file(version.cover_storage_path), f"{Path(version.original_filename).stem}.pdf"

    async def _get_public_version(self, template_public_id: str):
        asset = await self._templates.get_by_public_id(template_public_id)
        if asset is None:
            raise TemplateNotFoundError()
        if asset.visibility != "public" or asset.status != "active":
            raise TemplateNotPublicError()
        if asset.current_version_id is None:
            raise TemplateVersionMissingError()
        version = await self._versions.get_active(asset.current_version_id)
        if version is None:
            raise TemplateVersionMissingError()
        return asset, version

    async def _read_bytes(self, storage_path: str) -> bytes:
        try:
            return await self._storage.read_bytes(storage_path)
        except FileNotFoundError as exc:
            raise TemplateVersionMissingError() from exc
