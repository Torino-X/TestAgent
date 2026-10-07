"""Project source binding and upload service."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.models.project import ProjectSource
from app.repositories.file_repository import FileRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.project_source_repository import ProjectSourceRepository
from app.services.library_service import LibraryService
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id


logger = logging.getLogger(__name__)


_ROLE_ALIASES = {
    "api": "api_spec",
    "history": "historical_test",
}
_VALID_ROLES = {
    "requirement", "api_spec", "design", "technical_spec",
    "historical_test", "defect_history", "other",
}


class ProjectSourceService:
    def __init__(self, session: AsyncSession, *, index_service=None) -> None:
        self._session = session
        self._projects = ProjectRepository(session)
        self._sources = ProjectSourceRepository(session)
        self._files = FileRepository(session)
        if index_service is None:
            from app.context_engine.indexing.document_service import IndexDocumentService

            index_service = IndexDocumentService(session)
        self._index_service = index_service

    async def list_sources(self, project_public_id: str, user_id: int) -> list[dict]:
        project = await self._require_project(project_public_id, user_id)
        return [self._to_preview(source, uploaded) for source, uploaded in await self._sources.list_with_files(project.id)]

    async def attach(
        self,
        project_public_id: str,
        user_id: int,
        file_public_id: str,
        *,
        source_role: str = "other",
        added_from: str = "library",
    ) -> dict:
        project = await self._require_project(project_public_id, user_id)
        uploaded = await self._files.get_by_public_id(user_id, file_public_id)
        if uploaded is None:
            raise NotFoundError("资料库文件")
        role = self._normalize_role(source_role)
        now = utcnow()
        source = await self._sources.get_any_by_project_file(project.id, uploaded.id)
        if source is None:
            source = await self._sources.create(ProjectSource(
                public_id=generate_public_id("project_source"),
                project_id=project.id,
                uploaded_file_id=uploaded.id,
                added_by_user_id=user_id,
                source_role=role,
                is_current=True,
                status="active",
                added_from=added_from,
                created_at=now,
                updated_at=now,
            ))
        else:
            source.source_role = role
            source.added_from = added_from
            source.is_current = True
            source.status = "active"
            source.deleted_at = None
            source.updated_at = now
        await self._index_source(project, source, uploaded)
        await self._projects.touch(project.id, now)
        self._register_cache_invalidation(user_id, project_public_id)
        return self._to_preview(source, uploaded)

    async def upload(
        self,
        project_public_id: str,
        user_id: int,
        content: bytes,
        file_name: str,
        content_type: str | None,
        source_role: str = "other",
    ) -> dict:
        await self._require_project(project_public_id, user_id)
        item = await LibraryService(self._session).upload_file(content, file_name, user_id, content_type)
        return await self.attach(
            project_public_id,
            user_id,
            str(item["id"]),
            source_role=source_role,
            added_from="project_upload",
        )

    async def update(self, project_public_id: str, source_public_id: str, user_id: int, *, source_role: str | None, is_current: bool | None) -> dict:
        project = await self._require_project(project_public_id, user_id)
        source = await self._sources.get_active(project.id, source_public_id)
        if source is None:
            raise NotFoundError("项目来源")
        if source_role is not None:
            source.source_role = self._normalize_role(source_role)
        if is_current is not None:
            source.is_current = is_current
        source.updated_at = utcnow()
        # Resolve the related file by internal id without exposing storage metadata.
        from sqlalchemy import select
        from app.models.uploaded_file import UploadedFile
        uploaded = (await self._session.execute(
            select(UploadedFile).where(UploadedFile.id == source.uploaded_file_id)
        )).scalar_one()
        if source.is_current:
            await self._index_source(project, source, uploaded)
        else:
            await self._invalidate_source(project, uploaded.public_id, user_id)
        await self._projects.touch(project.id, source.updated_at)
        self._register_cache_invalidation(user_id, project_public_id)
        return self._to_preview(source, uploaded)

    async def remove(self, project_public_id: str, source_public_id: str, user_id: int) -> None:
        project = await self._require_project(project_public_id, user_id)
        source = await self._sources.get_active(project.id, source_public_id)
        if source is None:
            raise NotFoundError("项目来源")
        from sqlalchemy import select
        from app.models.uploaded_file import UploadedFile

        uploaded = (await self._session.execute(
            select(UploadedFile).where(UploadedFile.id == source.uploaded_file_id)
        )).scalar_one_or_none()
        now = utcnow()
        source.status = "removed"
        source.is_current = False
        source.deleted_at = now
        source.updated_at = now
        if uploaded is not None:
            await self._invalidate_source(project, uploaded.public_id, user_id)
        await self._projects.touch(project.id, now)
        self._register_cache_invalidation(user_id, project_public_id)

    async def _index_source(self, project, source, uploaded) -> None:
        """Submit the shared library object into the Project-private namespace.

        Indexing is fail-open and uses a savepoint so a broken optional index
        backend cannot roll back the source binding transaction.
        """
        try:
            async with self._session.begin_nested():
                await self._index_service.submit_uploaded_file(
                    user_id=project.user_id,
                    file_public_id=uploaded.public_id,
                    workspace_key=project.context_workspace_key,
                    metadata={
                        "project_source_id": source.public_id,
                        "source_role": source.source_role,
                        "is_current": bool(source.is_current),
                    },
                    commit=False,
                )
        except Exception as exc:  # noqa: BLE001 — optional RAG must not block binding
            logger.warning(
                "Project source indexing degraded | project=%s | source=%s | err=%s",
                project.public_id,
                source.public_id,
                type(exc).__name__,
            )

    async def _invalidate_source(self, project, file_public_id: str, user_id: int) -> None:
        try:
            from app.repositories.context_engine_repositories import ContextIndexDocumentRepository

            async with self._session.begin_nested():
                await ContextIndexDocumentRepository(
                    self._session
                ).invalidate_uploaded_file_in_workspace(
                    user_id=user_id,
                    file_public_id=file_public_id,
                    workspace_key=project.context_workspace_key,
                    now=utcnow(),
                )
        except Exception as exc:  # noqa: BLE001 — relation lifecycle remains authoritative
            logger.warning(
                "Project source index invalidation degraded | project=%s | file=%s | err=%s",
                project.public_id,
                file_public_id,
                type(exc).__name__,
            )

    async def _require_project(self, public_id: str, user_id: int):
        project = await self._projects.get_owned(public_id, user_id)
        if project is None:
            raise NotFoundError("项目")
        return project

    @staticmethod
    def _normalize_role(role: str) -> str:
        normalized = _ROLE_ALIASES.get(role, role)
        if normalized not in _VALID_ROLES:
            raise ValidationError("source_role 不受支持")
        return normalized

    def _register_cache_invalidation(self, user_id: int, project_public_id: str) -> None:
        from app.cache.domains.project_cache import get_project_cache
        from app.db.sync import register_after_commit

        async def invalidate(_session):
            await get_project_cache().invalidate(user_id, project_public_id)

        register_after_commit(self._session, invalidate)

    @staticmethod
    def _to_preview(source, uploaded) -> dict:
        return {
            "id": source.public_id,
            "fileId": uploaded.public_id,
            "name": uploaded.original_name,
            "description": f"{source.source_role} · {'项目上传' if source.added_from == 'project_upload' else '从资料库添加'}",
            "sourceRole": source.source_role,
            "isCurrent": bool(source.is_current),
            "addedFrom": source.added_from,
            "updatedAt": source.updated_at.isoformat() if source.updated_at else "",
        }
