"""索引文档 Ingest：uploaded_files → context_index_documents → enqueue job。

CE-03 WP-3：source_digest 幂等（同 digest no-op）；digest 变化 → 新文档行 +
旧行 superseded + delete_external job；不支持类型/超限 → rejected（不伪解析）。
owner-scope：仅当前 user 的文件可索引。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context_engine.indexing.chunker import CHUNK_POLICY_KEY

SUPPORTED_EXTENSIONS = {".txt", ".md", ".docx", ".json"}
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB

CHUNK_OPERATION = "parse_chunk"
EMBED_OPERATION = "embed_document"
LEXICAL_OPERATION = "index_lexical"
DELETE_EXTERNAL_OPERATION = "delete_external"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _gen_public_id(prefix: str) -> str:
    import uuid

    return prefix + uuid.uuid4().hex[:40]


def compute_source_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def compute_job_idempotency_key(
    *,
    document_public_id: str,
    source_version: str,
    source_digest: str,
    operation: str,
    chunk_policy_key: str,
    external_index_namespace: str | None,
) -> str:
    parts = [
        document_public_id,
        source_version,
        source_digest,
        operation,
        chunk_policy_key,
        external_index_namespace or "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class DocumentIndexError(Exception):
    """文档索引错误（安全 detail）。"""

    def __init__(self, detail: str, code: str = "context.index.document_error") -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


class IndexDocumentService:
    """文档索引入口：幂等提交 + 状态机推进。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def submit_uploaded_file(
        self,
        *,
        user_id: int,
        file_public_id: str,
        workspace_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """提交一个 uploaded_files 索引。

        返回 {document_public_id, status, created, superseded}。
        owner-scope：文件必须属于 user_id，否则 DocumentIndexError。
        """
        from app.models.uploaded_file import UploadedFile
        from app.repositories.context_engine_repositories import ContextIndexDocumentRepository, ContextIndexJobRepository
        from app.repositories.file_repository import FileRepository

        file_repo = FileRepository(self._session)
        uploaded = await file_repo.get_by_public_id(user_id, file_public_id)
        if uploaded is None:
            raise DocumentIndexError("文件不存在或不属于当前用户", code="context.index.file_not_found")

        ext = ("." + uploaded.file_ext).lower() if uploaded.file_ext else None
        if ext not in SUPPORTED_EXTENSIONS:
            raise DocumentIndexError(
                f"不支持的文件类型 {ext or 'unknown'}（支持 txt/md/docx/json）",
                code="context.index.unsupported_type",
            )
        if uploaded.file_size and uploaded.file_size > MAX_FILE_BYTES:
            raise DocumentIndexError(
                f"文件超过大小上限 {MAX_FILE_BYTES // (1024 * 1024)}MB",
                code="context.index.file_too_large",
            )

        raw = self._read_file_bytes(uploaded.storage_path, uploaded.storage_type)
        source_digest = compute_source_digest(raw)

        conv_public_id = await self._conversation_public_id(uploaded.conversation_id)
        effective_workspace_key = (
            workspace_key
            if workspace_key is not None
            else (f"conversation:{conv_public_id}" if conv_public_id else None)
        )
        doc_repo = ContextIndexDocumentRepository(self._session)
        existing = await doc_repo.get_by_source_in_workspace(
            user_id,
            "uploaded_file",
            uploaded.public_id,
            effective_workspace_key,
        )

        # 同 digest → no-op（幂等）
        if existing and existing.source_digest == source_digest:
            profile_metadata = await self._safe_uploaded_file_profile_metadata(uploaded)
            existing.metadata_json = (
                dict(existing.metadata_json or {})
                | dict(profile_metadata or {})
                | dict(metadata or {})
            ) or None
            await self._session.flush()
            if commit:
                await self._session.commit()
            return {
                "document_public_id": existing.public_id,
                "status": existing.status,
                "created": False,
                "superseded": False,
            }

        # digest 不同或不存在 → 创建/换代
        if existing and existing.status != "superseded":
            existing.status = "superseded"
            existing.deleted_at = _utcnow()
            await self._session.flush()
            # 清理旧外部副本
            await self._enqueue_job(
                user_id=user_id,
                document=existing,
                operation=DELETE_EXTERNAL_OPERATION,
                chunk_policy_key=existing.chunk_policy_key,
            )

        profile_metadata = await self._safe_uploaded_file_profile_metadata(uploaded)
        document_metadata = dict(profile_metadata or {}) | dict(metadata or {})
        doc = self._new_document(
            user_id=user_id,
            source_public_id=uploaded.public_id,
            source_version=str((int(existing.source_version or "0") + 1)) if existing else "1",
            source_digest=source_digest,
            title=uploaded.original_name,
            workspace_key=effective_workspace_key,
            metadata_json=document_metadata or None,
        )
        from app.repositories.base import ensure_model_id

        await ensure_model_id(self._session, type(doc), doc)
        self._session.add(doc)
        await self._session.flush()

        await self._enqueue_job(
            user_id=user_id,
            document=doc,
            operation=CHUNK_OPERATION,
            chunk_policy_key=CHUNK_POLICY_KEY,
        )
        if commit:
            await self._session.commit()

        return {
            "document_public_id": doc.public_id,
            "status": doc.status,
            "created": True,
            "superseded": bool(existing and existing.status == "superseded"),
        }

    async def submit_artifact(
        self,
        *,
        user_id: int,
        artifact_public_id: str,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Submit a completed artifact to the same versioned index pipeline.

        A generated test plan is not an ``uploaded_file``.  Its artifact id,
        version and digest remain the source of truth so every retrieved chunk
        can be traced back to a downloadable output version.
        """
        from app.models.artifact import Artifact
        from app.models.conversation import Conversation
        from app.repositories.context_engine_repositories import ContextIndexDocumentRepository

        artifact = (await self._session.execute(
            select(Artifact).where(
                Artifact.public_id == artifact_public_id,
                Artifact.user_id == user_id,
                Artifact.status == "available",
                Artifact.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if artifact is None:
            raise DocumentIndexError(
                "产物不存在、不可用或不属于当前用户",
                code="context.index.artifact_not_found",
            )
        ext = artifact.file_ext.lower().lstrip(".")
        if f".{ext}" not in SUPPORTED_EXTENSIONS:
            raise DocumentIndexError(
                f"不支持索引的产物类型 .{ext or 'unknown'}",
                code="context.index.unsupported_type",
            )
        if artifact.file_size and artifact.file_size > MAX_FILE_BYTES:
            raise DocumentIndexError(
                f"产物超过大小上限 {MAX_FILE_BYTES // (1024 * 1024)}MB",
                code="context.index.file_too_large",
            )

        conversation = await self._session.get(Conversation, artifact.conversation_id)
        if conversation is None or conversation.user_id != user_id:
            raise DocumentIndexError(
                "产物所属会话不可用",
                code="context.index.artifact_conversation_not_found",
            )
        workspace_key = await self._artifact_workspace_key(artifact, conversation)
        raw = self._read_file_bytes(artifact.storage_path, artifact.storage_type)
        digest = artifact.input_hash or artifact.file_hash or compute_source_digest(raw)
        source_version = str(int(artifact.version_no or 1))
        metadata = self._artifact_metadata(artifact)
        docs = ContextIndexDocumentRepository(self._session)
        existing = await docs.get_by_source_in_workspace(
            user_id, "artifact", artifact.public_id, workspace_key
        )
        if existing and existing.source_digest == digest and existing.source_version == source_version:
            existing.metadata_json = dict(existing.metadata_json or {}) | metadata
            await self._session.flush()
            if commit:
                await self._session.commit()
            return {
                "document_public_id": existing.public_id,
                "status": existing.status,
                "created": False,
                "superseded": False,
            }
        if existing and existing.status != "superseded":
            existing.status = "superseded"
            existing.deleted_at = _utcnow()
            await self._session.flush()
            await self._enqueue_job(
                user_id=user_id,
                document=existing,
                operation=DELETE_EXTERNAL_OPERATION,
                chunk_policy_key=existing.chunk_policy_key,
            )

        doc = self._new_document(
            user_id=user_id,
            source_type="artifact",
            source_public_id=artifact.public_id,
            source_version=source_version,
            source_digest=digest,
            title=artifact.file_name,
            workspace_key=workspace_key,
            metadata_json=metadata,
        )
        from app.repositories.base import ensure_model_id

        await ensure_model_id(self._session, type(doc), doc)
        self._session.add(doc)
        await self._session.flush()
        await self._enqueue_job(
            user_id=user_id,
            document=doc,
            operation=CHUNK_OPERATION,
            chunk_policy_key=CHUNK_POLICY_KEY,
        )
        if commit:
            await self._session.commit()
        return {
            "document_public_id": doc.public_id,
            "status": doc.status,
            "created": True,
            "superseded": existing is not None,
        }

    async def _artifact_workspace_key(self, artifact, conversation) -> str:
        if artifact.project_id:
            from app.models.project import Project

            project = await self._session.get(Project, artifact.project_id)
            if project is not None and project.user_id == artifact.user_id and project.deleted_at is None:
                return str(project.context_workspace_key)
        return f"conversation:{conversation.public_id}"

    @staticmethod
    def _artifact_metadata(artifact) -> dict[str, Any]:
        source = dict(artifact.metadata_json or {})
        section_package = source.get("test_plan_content") or {}
        sections = section_package.get("section_package", {}).get("generated_sections", [])
        return {
            "artifact_id": artifact.public_id,
            "artifact_type": artifact.artifact_type,
            "artifact_version": int(artifact.version_no or 1),
            "artifact_hash": artifact.input_hash or artifact.file_hash,
            "task_id": artifact.task_id,
            "project_id": artifact.project_id,
            "section_ids": [str(item.get("section_id") or item.get("id")) for item in sections if isinstance(item, dict) and (item.get("section_id") or item.get("id"))],
            "source_role": "generated_artifact",
            "is_current": True,
        }

    async def sync_uploaded_file_profile_metadata(self, *, user_id: int, file_public_id: str) -> int:
        from app.repositories.context_engine_repositories import ContextIndexDocumentRepository
        from app.repositories.file_repository import FileRepository

        uploaded = await FileRepository(self._session).get_by_public_id(user_id, file_public_id)
        if uploaded is None:
            return 0
        metadata = await self._safe_uploaded_file_profile_metadata(uploaded)
        return await ContextIndexDocumentRepository(self._session).sync_uploaded_file_metadata(
            user_id=user_id,
            file_public_id=file_public_id,
            metadata=metadata,
        )

    async def _safe_uploaded_file_profile_metadata(self, uploaded) -> dict[str, Any]:
        from app.repositories.attachment_understanding_repository import FileSemanticProfileRepository

        return await FileSemanticProfileRepository(self._session).safe_index_metadata_for_file(
            uploaded_file=uploaded,
        )

    async def _conversation_public_id(self, conversation_id: int | None) -> str | None:
        if conversation_id is None:
            return None
        from app.models.conversation import Conversation

        result = await self._session.execute(
            select(Conversation.public_id).where(Conversation.id == conversation_id)
        )
        return result.scalar_one_or_none()

    async def _enqueue_job(
        self,
        *,
        user_id: int,
        document,
        operation: str,
        chunk_policy_key: str,
    ) -> None:
        from app.repositories.context_engine_repositories import ContextIndexJobRepository

        job_repo = ContextIndexJobRepository(self._session)
        idem = compute_job_idempotency_key(
            document_public_id=document.public_id,
            source_version=document.source_version,
            source_digest=document.source_digest,
            operation=operation,
            chunk_policy_key=chunk_policy_key,
            external_index_namespace=document.external_index_namespace,
        )
        existing = await job_repo.get_by_idempotency_key(idem)
        if existing:
            return
        from app.models.context_engine import ContextIndexJob
        from app.repositories.base import ensure_model_id

        job = ContextIndexJob(
            public_id=_gen_public_id("idx_"),
            user_id=user_id,
            document_id=document.id,
            operation=operation,
            status="pending",
            priority=100,
            attempt=0,
            max_attempts=3,
            idempotency_key=idem,
        )
        await ensure_model_id(self._session, type(job), job)
        self._session.add(job)

    @staticmethod
    def _read_file_bytes(storage_path: str, storage_type: str = "local") -> bytes:
        """读取已落盘文件。

        必须通过 LocalFileStorageService 解析相对路径，
        否则 raw open() 会按 cwd 找文件而 FileService 按 _base 写入，
        二者 cwd/_base 不一致时 FileNotFoundError。

        安全策略：
        - 绝对路径（Path.is_absolute()，含 Windows 盘符）：允许
        - 相对路径：必须解析到 local_storage._base 内（防 ../ 越界）
        - 根相对路径（Windows 中 `/foo` 这种）：按相对路径处理，强制 _base 越界检查
        """
        if storage_type == "oss":
            from app.storage.oss_storage import object_storage

            try:
                return object_storage.read_bytes_sync(storage_path)
            except FileNotFoundError as exc:
                raise DocumentIndexError(
                    "文件存储缺失",
                    code="context.index.file_missing",
                ) from exc

        # Legacy local rows are supported only until the explicit OSS migration
        # command has copied their objects.
        from app.storage.local_storage import local_storage

        # 兼容测试 fixture 传入 pathlib.Path 的情况
        path = Path(storage_path) if isinstance(storage_path, Path) else Path(storage_path)

        # Windows 中 `/foo` 不是 absolute，会被解析到当前盘的根；
        # 这种根相对路径应走 base-prefix 分支以防越界。
        if path.is_absolute() and not str(storage_path).startswith(("/", "\\")):
            full_path = path
        else:
            # 相对路径 / 根相对路径：拼接 _base，然后做路径越界检查
            full_path = local_storage._base / storage_path  # noqa: SLF001
            try:
                full_path.resolve().relative_to(local_storage._base.resolve())
            except ValueError as exc:
                raise DocumentIndexError(
                    "storage_path 越界",
                    code="context.index.path_traversal",
                ) from exc
        if not full_path.exists():
            raise DocumentIndexError(
                "文件存储缺失",
                code="context.index.file_missing",
            )
        try:
            return full_path.read_bytes()
        except OSError as exc:
            raise DocumentIndexError(
                f"文件读取失败: {type(exc).__name__}",
                code="context.index.file_read_error",
            ) from exc

    @staticmethod
    def _new_document(
        *,
        user_id: int,
        source_type: str = "uploaded_file",
        source_public_id: str,
        source_version: str,
        source_digest: str,
        title: str | None,
        workspace_key: str | None,
        metadata_json: dict[str, Any] | None = None,
    ):
        from app.models.context_engine import ContextIndexDocument

        return ContextIndexDocument(
            public_id=_gen_public_id("idoc_"),
            user_id=user_id,
            workspace_key=workspace_key,
            source_type=source_type,
            source_public_id=source_public_id,
            source_version=source_version,
            source_digest=source_digest,
            title=title,
            status="pending",
            chunk_policy_key=CHUNK_POLICY_KEY,
            chunk_policy_version="v1",  # NOT NULL 列；版本语义由 chunk_policy_key 组合承载
            lexical_index_status="pending",
            vector_index_status="pending",
            metadata_json=metadata_json,
            idempotency_key=_gen_public_id("idem_"),
        )
# auto-appended module-level note: document service: 索引文档入库(create / update / delete)。
