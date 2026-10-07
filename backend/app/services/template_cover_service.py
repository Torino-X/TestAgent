"""One-time generation and durable storage of template card covers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.template_asset import TemplateAsset
from app.models.template_version import TemplateVersion
from app.services.document_preview_service import DocumentPreviewService
from app.storage.oss_storage import object_storage
from app.utils.datetime import utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _BuiltCover:
    kind: str
    storage_path: str | None = None
    size: int | None = None
    payload: dict[str, Any] | None = None


class TemplateCoverService:
    """Builds a cover once for an immutable template version.

    DOCX versions store a private PDF derivative. XLSX versions store only the
    bounded first-sheet display model used by the card. Request-time code reads
    these fields and never launches Office or reparses the canonical workbook.
    """

    def __init__(self, session: AsyncSession, *, storage=None) -> None:
        self._session = session
        self._storage = storage or object_storage
        self._document_preview = DocumentPreviewService(session)

    async def prepare_uploaded_version(
        self,
        version: TemplateVersion,
        *,
        owner_user_id: int,
        content: bytes,
    ) -> str | None:
        """Persist a ready cover before the upload transaction can succeed."""
        version.cover_status = "processing"
        version.cover_error = None
        built: _BuiltCover | None = None
        try:
            built = await self._build(version, owner_user_id=owner_user_id, content=content)
            self._apply_ready(version, built)
            await self._session.flush()
            return built.storage_path
        except Exception as exc:
            if built and built.storage_path:
                await self._storage.delete_file(built.storage_path)
            version.cover_status = "failed"
            version.cover_error = type(exc).__name__
            raise

    async def _build(
        self,
        version: TemplateVersion,
        *,
        owner_user_id: int,
        content: bytes,
    ) -> _BuiltCover:
        extension = version.file_ext.lower().lstrip(".")
        if extension == "docx":
            pdf = await asyncio.to_thread(self._document_preview._docx_to_pdf, content)
            stored = await self._storage.save_preview(
                pdf,
                f"{Path(version.original_filename).stem}.pdf",
                str(owner_user_id),
                version.public_id,
            )
            if not await self._storage.exists(stored.storage_path):
                await self._storage.delete_file(stored.storage_path)
                raise RuntimeError("Persisted template cover is not readable")
            return _BuiltCover(
                kind="pdf",
                storage_path=stored.storage_path,
                size=stored.file_size,
            )
        if extension == "xlsx":
            sheet = self._document_preview._parse_spreadsheet(content, ".xlsx")
            return _BuiltCover(kind="spreadsheet", payload=sheet.model_dump(mode="json"))
        raise ValueError(f"Unsupported template cover extension: {extension}")

    @staticmethod
    def _apply_ready(version: TemplateVersion, built: _BuiltCover) -> None:
        version.cover_status = "ready"
        version.cover_kind = built.kind
        version.cover_storage_path = built.storage_path
        version.cover_size = built.size
        version.cover_payload_json = built.payload
        version.cover_error = None
        version.cover_generated_at = utcnow()

    @classmethod
    async def backfill_missing(cls, limit: int = 4) -> None:
        """Generate a bounded batch for versions created before this feature.

        A conditional status update is the durable cross-worker claim. Failed
        conversions remain failed and are not retried on every page visit.
        """
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            await cls(session).backfill_missing_batch(limit=limit)

    async def backfill_missing_batch(self, limit: int = 4) -> None:
        rows = await self._session.execute(
            select(TemplateVersion.id, TemplateAsset.owner_user_id)
            .join(TemplateAsset, TemplateAsset.id == TemplateVersion.template_id)
            .where(
                TemplateVersion.cover_status == "missing",
                TemplateVersion.deleted_at.is_(None),
                TemplateAsset.deleted_at.is_(None),
            )
            .order_by(TemplateVersion.created_at.asc(), TemplateVersion.id.asc())
            .limit(max(1, min(limit, 20)))
        )
        for version_id, owner_user_id in rows.all():
            claim = await self._session.execute(
                update(TemplateVersion)
                .where(
                    TemplateVersion.id == version_id,
                    TemplateVersion.cover_status == "missing",
                )
                .values(cover_status="processing", cover_error=None)
            )
            await self._session.commit()
            if not claim.rowcount:
                continue

            stored_path: str | None = None
            try:
                version = await self._session.get(TemplateVersion, version_id)
                if version is None:
                    continue
                content = await self._storage.read_bytes(version.storage_path)
                built = await self._build(
                    version,
                    owner_user_id=owner_user_id,
                    content=content,
                )
                stored_path = built.storage_path
                self._apply_ready(version, built)
                await self._session.commit()
                logger.info(
                    "template.cover.ready | version=%s | kind=%s | bytes=%s",
                    version.public_id,
                    built.kind,
                    built.size or 0,
                )
            except Exception as exc:  # noqa: BLE001 - durable failure state
                await self._session.rollback()
                if stored_path:
                    await self._storage.delete_file(stored_path)
                version = await self._session.get(TemplateVersion, version_id)
                if version is not None:
                    version.cover_status = "failed"
                    version.cover_error = type(exc).__name__
                    await self._session.commit()
                logger.exception(
                    "template.cover.failed | version_id=%s",
                    version_id,
                    exc_info=exc,
                )


def serialize_template_cover(version: TemplateVersion, *, pdf_url: str) -> dict[str, Any]:
    status = version.cover_status or "missing"
    ready = status == "ready"
    return {
        "status": status,
        "kind": version.cover_kind if ready else None,
        "url": pdf_url if ready and version.cover_kind == "pdf" and version.cover_storage_path else None,
        "spreadsheet": version.cover_payload_json if ready and version.cover_kind == "spreadsheet" else None,
    }


def page_needs_cover_backfill(page: dict[str, Any]) -> bool:
    return any(
        (item.get("cover") or {}).get("status") == "missing"
        for item in page.get("items", [])
    )
