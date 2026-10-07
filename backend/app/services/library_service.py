"""Owner-scoped library aggregation and lifecycle operations."""

from __future__ import annotations

import re
import logging
from datetime import timedelta
from typing import Literal

from sqlalchemy import select

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.models.uploaded_file import UploadedFile
from app.models.project import Project
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.file_repository import FileRepository
from app.repositories.library_repository import LibraryRepository
from app.schemas.library import LibraryItem
from app.services.file_capability_registry import FileProcessingCapabilityRegistry
from app.storage.oss_storage import object_storage
from app.utils.datetime import utcnow
from app.utils.file_utils import readable_size, safe_ext
from app.utils.ids import generate_public_id


LibraryCategory = Literal["all", "image", "file"]
LibraryScope = Literal["active", "deleted"]
LibrarySourceFilter = Literal["all", "upload", "generated"]
LibraryFileType = Literal["all", "image", "document", "spreadsheet", "presentation", "pdf"]
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# Compatibility alias for callers/tests that historically patched this module
# symbol.  It deliberately points to OSS, never to durable local storage.
local_storage = object_storage
logger = logging.getLogger(__name__)


class LibraryService:
    """Maps existing records into a safe, owner-scoped library contract.

    Deleted items stay in the database and storage for 30 days. A lightweight
    expiry sweep runs whenever a library request is made, so no separate job is
    required for the first deployment while still guaranteeing eventual purge.
    """

    def __init__(self, session) -> None:
        self._session = session
        self._file_repo = FileRepository(session)
        self._artifact_repo = ArtifactRepository(session)
        self._library_repo = LibraryRepository(session)

    async def list_items(
        self,
        user_internal_id: int,
        *,
        category: LibraryCategory = "all",
        query: str = "",
        scope: LibraryScope = "active",
        source: LibrarySourceFilter = "all",
        file_type: LibraryFileType = "all",
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict], int]:
        from app.cache.domains.library_cache import (
            LibraryListDTO,
            filter_hash_for,
            get_library_cache,
        )

        async def _load() -> LibraryListDTO:
            items, total = await self._list_items_uncached(
                user_internal_id,
                category=category,
                query=query,
                scope=scope,
                source=source,
                file_type=file_type,
                page=page,
                page_size=page_size,
            )
            return LibraryListDTO(items=items, total=total)

        cached = await get_library_cache().get_or_load_list(
            user_internal_id,
            _load,
            filter_hash=filter_hash_for(
                category=category,
                query=query,
                scope=scope,
                source=source,
                file_type=file_type,
                page=page,
                page_size=page_size,
            ),
        )
        return cached.items, cached.total

    async def _list_items_uncached(
        self,
        user_internal_id: int,
        *,
        category: LibraryCategory,
        query: str,
        scope: LibraryScope,
        source: LibrarySourceFilter,
        file_type: LibraryFileType,
        page: int,
        page_size: int,
    ) -> tuple[list[dict], int]:
        # Phase 0: bulk DELETE removed from the GET hot path.  Purging is
        # now owned by ``CacheMaintenanceService`` (driven from the
        # AgentExecutionWorker idle branch every 30s + a dedicated
        # CacheMaintenanceWorker every 60s as a safety net).  GET
        # requests are now read-only and connection-cheap.
        listing = await self._library_repo.list_metadata(
            user_internal_id,
            category=category,
            query=query,
            scope=scope,
            source=source,
            file_type=file_type,
            page=page,
            page_size=page_size,
        )
        return [self._from_metadata_row(row).model_dump(mode="json") for row in listing.rows], listing.total

    async def upload_file(
        self,
        content: bytes,
        file_name: str,
        user_internal_id: int,
        content_type: str | None = None,
    ) -> dict:
        """Store a user-owned library upload without creating a conversation."""
        settings = get_settings()
        if len(content) > settings.upload_max_bytes:
            raise ValidationError(f"文件超过上传大小上限 {readable_size(settings.upload_max_bytes)}")
        extension = safe_ext(file_name)
        capability = FileProcessingCapabilityRegistry().for_extension(extension)
        if capability.media_category == "unknown":
            raise ValidationError("不支持的文件类型")
        stored = await object_storage.save_upload(
            content, file_name, user_id=str(user_internal_id), conversation_id="library"
        )
        now = utcnow()
        upload = UploadedFile(
            public_id=generate_public_id("file"),
            user_id=user_internal_id,
            conversation_id=None,
            original_name=file_name,
            stored_name=stored.file_name,
            file_ext=extension,
            mime_type=content_type or None,
            file_size=len(content),
            file_type="unknown",
            upload_status="uploaded",
            storage_type=object_storage.storage_type,
            storage_path=stored.storage_path,
            created_at=now,
            updated_at=now,
        )
        upload = await self._file_repo.create(upload)
        await self._session.commit()
        # Phase 1 cache (P0 整改): library list generation bump — list 必须立刻失效,
        # 否则新增的 upload 在 60s TTL 内不可见. 失败仅记 metric,不影响主流程.
        try:
            from app.cache.domains.library_cache import get_library_cache

            await get_library_cache().bump_generation(user_internal_id)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "LibraryService.upload_file: bump_generation failed | user=%s | %s",
                user_internal_id, exc,
            )
        return self._from_upload(upload).model_dump(mode="json")

    async def get_item(self, item_public_id: str, user_internal_id: int) -> dict:
        item = await self._get_owned_item(item_public_id, user_internal_id, scope="active")
        return self._to_item(item).model_dump(mode="json")

    async def get_owned_storage_item(self, item_public_id: str, user_internal_id: int):
        """Return an owner-scoped record for internal preview services only.

        The storage path stays server-side: callers must not serialize this ORM
        object into an API response.
        """
        return await self._get_owned_item(item_public_id, user_internal_id, scope="active")

    async def rename(self, item_public_id: str, name: str, user_internal_id: int) -> dict:
        clean_name = self._validate_name(name)
        item = await self._get_owned_item(item_public_id, user_internal_id, scope="active")
        now = utcnow()
        if self._is_upload(item):
            await self._file_repo.rename(item.public_id, clean_name, now)
            item.original_name = clean_name
        else:
            await self._artifact_repo.rename(item.public_id, clean_name, now)
            item.file_name = clean_name
        item.updated_at = now
        await self._session.commit()
        await self._invalidate_project_cache(item, user_internal_id)
        # Phase 1 cache (P0 整改): rename 影响 list 行内容, bump_generation 即可让
        # 旧 list key 在 60s 内自然失效. 失败仅 log.
        try:
            from app.cache.domains.library_cache import get_library_cache

            await get_library_cache().bump_generation(user_internal_id)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "LibraryService.rename: bump_generation failed | user=%s | %s",
                user_internal_id, exc,
            )
        return self._to_item(item).model_dump(mode="json")

    async def soft_delete(self, item_public_id: str, user_internal_id: int) -> None:
        item = await self._get_owned_item(item_public_id, user_internal_id, scope="active")
        now = utcnow()
        if self._is_upload(item):
            await self._file_repo.soft_delete(item.public_id, now)
        else:
            await self._artifact_repo.soft_delete(item.public_id, now)
        await self._session.commit()
        await self._invalidate_project_cache(item, user_internal_id)
        # Phase 1 cache (P0 整改): soft_delete 改变 list active vs deleted 视图.
        try:
            from app.cache.domains.library_cache import get_library_cache

            await get_library_cache().bump_generation(user_internal_id)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "LibraryService.soft_delete: bump_generation failed | user=%s | %s",
                user_internal_id, exc,
            )

    async def restore(self, item_public_id: str, user_internal_id: int) -> dict:
        item = await self._get_owned_item(item_public_id, user_internal_id, scope="deleted")
        now = utcnow()
        if self._is_upload(item):
            await self._file_repo.restore(item.public_id, now)
        else:
            await self._artifact_repo.restore(item.public_id, now)
        item.deleted_at = None
        item.updated_at = now
        await self._session.commit()
        await self._invalidate_project_cache(item, user_internal_id)
        # Phase 1 cache (P0 整改): restore 同上,影响 list active vs deleted 视图.
        try:
            from app.cache.domains.library_cache import get_library_cache

            await get_library_cache().bump_generation(user_internal_id)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "LibraryService.restore: bump_generation failed | user=%s | %s",
                user_internal_id, exc,
            )
        return self._to_item(item).model_dump(mode="json")

    async def permanent_delete(self, item_public_id: str, user_internal_id: int) -> None:
        item = await self._get_owned_item(item_public_id, user_internal_id, scope="deleted")
        await self._delete_storage(item)
        from app.services.document_preview_service import DocumentPreviewService

        await DocumentPreviewService(self._session).purge_preview(item_public_id, user_internal_id)
        if self._is_upload(item):
            await self._file_repo.hard_delete(item.public_id)
        else:
            await self._artifact_repo.hard_delete(item.public_id)
        await self._session.commit()
        await self._invalidate_project_cache(item, user_internal_id)
        # Phase 1 cache (P0 整改): 物理删除也是 list 失效事件.
        try:
            from app.cache.domains.library_cache import get_library_cache

            await get_library_cache().bump_generation(user_internal_id)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "LibraryService.permanent_delete: bump_generation failed | "
                "user=%s | %s",
                user_internal_id, exc,
            )

    async def get_download_stream(self, item_public_id: str, user_internal_id: int) -> tuple:
        item = await self._get_owned_item(item_public_id, user_internal_id, scope="active")
        storage_path = getattr(item, "storage_path", "")
        if not storage_path or not await object_storage.exists(storage_path):
            raise NotFoundError("文件")

        async def stream():
            async for chunk in object_storage.open_file(storage_path):
                yield chunk

        return stream, self._item_name(item), getattr(item, "mime_type", None) or "application/octet-stream"

    async def _load_user_sources(self, user_internal_id: int, *, scope: LibraryScope):
        if scope == "deleted":
            return (
                await self._file_repo.list_deleted_by_user(user_internal_id),
                await self._artifact_repo.list_deleted_by_user(user_internal_id),
            )
        return (
            await self._file_repo.list_by_user(user_internal_id),
            await self._artifact_repo.list_by_user(user_internal_id),
        )

    async def _get_owned_item(self, item_public_id: str, user_internal_id: int, *, scope: LibraryScope):
        if item_public_id.startswith("file_"):
            item = await self._file_repo.get_any_by_public_id(item_public_id)
            if item is not None and item.user_id == user_internal_id and self._matches_scope(item, scope):
                return item
        elif item_public_id.startswith("art_"):
            item = await self._artifact_repo.get_any_by_public_id(item_public_id)
            if item is not None and item.user_id == user_internal_id and self._matches_scope(item, scope):
                return item
        raise NotFoundError("资料")

    async def _purge_expired_deleted(self, user_internal_id: int) -> None:
        cutoff = utcnow() - timedelta(days=30)
        uploads, artifacts = await self._load_user_sources(user_internal_id, scope="deleted")
        expired = [item for item in [*uploads, *artifacts] if item.deleted_at and item.deleted_at <= cutoff]
        if not expired:
            return
        for item in expired:
            await self._delete_storage(item)
            if self._is_upload(item):
                await self._file_repo.hard_delete(item.public_id)
            else:
                await self._artifact_repo.hard_delete(item.public_id)
        await self._session.commit()

    async def _delete_storage(self, item) -> None:
        storage_path = getattr(item, "storage_path", "")
        if storage_path:
            await object_storage.delete_file(storage_path)

    async def _invalidate_project_cache(self, item, user_internal_id: int) -> None:
        """Evict a Project detail snapshot after mutating one of its generated artifacts."""
        project_id = getattr(item, "project_id", None)
        if not project_id:
            return
        try:
            project_public_id = (await self._session.execute(
                select(Project.public_id).where(
                    Project.id == project_id,
                    Project.user_id == user_internal_id,
                )
            )).scalar_one_or_none()
            if project_public_id:
                from app.cache.domains.project_cache import get_project_cache

                await get_project_cache().invalidate(user_internal_id, project_public_id)
        except Exception as exc:  # noqa: BLE001 - cache failure must not roll back a committed file action
            logger.warning(
                "LibraryService: project cache invalidation failed | user=%s | project_id=%s | %s",
                user_internal_id, project_id, exc,
            )

    @staticmethod
    def _matches_scope(item, scope: LibraryScope) -> bool:
        return (item.deleted_at is not None) if scope == "deleted" else (item.deleted_at is None)

    @staticmethod
    def _is_upload(item) -> bool:
        return hasattr(item, "original_name")

    @staticmethod
    def _item_name(item) -> str:
        return item.original_name if LibraryService._is_upload(item) else item.file_name

    @staticmethod
    def _validate_name(name: str) -> str:
        result = name.strip()
        if not result or len(result) > 255 or "/" in result or "\\" in result or _CONTROL_CHARS.search(result):
            raise ValidationError("文件名不合法")
        return result

    @staticmethod
    def _is_image(mime_type: str | None, extension: str | None) -> bool:
        if (mime_type or "").lower().startswith("image/"):
            return True
        return (extension or "").lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

    def _file_type_for(self, item: LibraryItem) -> LibraryFileType:
        extension = item.extension.lower()
        if item.kind == "image":
            return "image"
        if extension == ".pdf":
            return "pdf"
        if extension in {".xls", ".xlsx", ".csv", ".ods"}:
            return "spreadsheet"
        if extension in {".ppt", ".pptx", ".odp"}:
            return "presentation"
        return "document"

    def _to_item(self, item) -> LibraryItem:
        return self._from_upload(item) if self._is_upload(item) else self._from_artifact(item)

    def _from_upload(self, upload) -> LibraryItem:
        kind = "image" if self._is_image(upload.mime_type, upload.file_ext) else "file"
        return LibraryItem(
            id=upload.public_id,
            name=upload.original_name,
            kind=kind,
            source="upload",
            mime_type=upload.mime_type,
            extension=upload.file_ext or "",
            size_bytes=upload.file_size,
            modified_at=upload.updated_at or upload.created_at,
            download_url=f"/api/library/items/{upload.public_id}/download",
            deleted_at=upload.deleted_at,
        )

    def _from_artifact(self, artifact) -> LibraryItem:
        kind = "image" if self._is_image(artifact.mime_type, artifact.file_ext) else "file"
        return LibraryItem(
            id=artifact.public_id,
            name=artifact.file_name,
            kind=kind,
            source="generated",
            mime_type=artifact.mime_type,
            extension=artifact.file_ext or "",
            size_bytes=artifact.file_size,
            modified_at=artifact.updated_at or artifact.created_at,
            artifact_type=artifact.artifact_type,
            download_url=f"/api/library/items/{artifact.public_id}/download",
            deleted_at=artifact.deleted_at,
        )

    def _from_metadata_row(self, row) -> LibraryItem:
        kind = "image" if self._is_image(row.mime_type, row.extension) else "file"
        return LibraryItem(
            id=row.public_id,
            name=row.name,
            kind=kind,
            source=row.source,
            mime_type=row.mime_type,
            extension=row.extension,
            size_bytes=row.size_bytes,
            modified_at=row.modified_at,
            artifact_type=row.artifact_type,
            download_url=f"/api/library/items/{row.public_id}/download",
            deleted_at=row.deleted_at,
        )
