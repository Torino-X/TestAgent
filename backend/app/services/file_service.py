"""File service — upload, confirm type, delete, backed by real storage + DB."""

from __future__ import annotations

import asyncio
import logging
import mimetypes

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.uploaded_file import UploadedFile
from app.repositories.context_engine_repositories import ContextIndexDocumentRepository
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.file_repository import FileRepository
from app.services.file_capability_registry import FileProcessingCapabilityRegistry
from app.storage.oss_storage import object_storage
from app.utils.datetime import utcnow
from app.utils.file_utils import readable_size, safe_ext
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)


class FileService:
    """File business logic — real filesystem writes and DB metadata."""

    def __init__(self, session: AsyncSession, *, storage=None, context_llm_invoker=None) -> None:
        self._session = session
        self._file_repo = FileRepository(session)
        self._conv_repo = ConversationRepository(session)
        self._storage = storage or object_storage
        self._context_llm_invoker = context_llm_invoker

    async def upload(
        self,
        content: bytes,
        file_name: str,
        user_internal_id: int,
        conv_public_id: str,
        content_type: str | None = None,
    ) -> dict:
        # Resolve conversation public_id → internal id
        conv = await self._conv_repo.get_by_public_id(conv_public_id)
        if not conv or conv.user_id != user_internal_id or conv.deleted_at is not None:
            raise NotFoundError("会话")

        settings = get_settings()
        if len(content) > settings.upload_max_bytes:
            raise ValidationError(f"文件超过上传大小上限 {readable_size(settings.upload_max_bytes)}")

        ext = safe_ext(file_name)
        capability = FileProcessingCapabilityRegistry().for_extension(ext)
        if capability.media_category == "unknown":
            raise ValidationError("不支持的文件类型")
        file_type = self._guess_type(ext)
        mime_type = self._resolve_mime_type(file_name, content_type)

        stored = await self._storage.save_upload(
            content, file_name,
            user_id=str(user_internal_id),
            conversation_id=conv_public_id,
        )

        file_orm = UploadedFile(
            public_id=generate_public_id("file"),
            user_id=user_internal_id,
            conversation_id=conv.id,
            original_name=file_name,
            stored_name=stored.file_name,
            file_ext=ext,
            mime_type=mime_type,
            file_size=len(content),
            file_hash=None,
            file_type=file_type,
            upload_status="uploaded",
            storage_type=self._storage.storage_type,
            storage_path=stored.storage_path,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        file_orm = await self._file_repo.create(file_orm)
        # CPS-01：上传成功后自动触发 Internal RAG 索引；
        # 索引失败（不支持类型/超限/重复）记 warning，不阻塞上传返回值。
        # 用户感知到的是文件已上传；RAG 检索可用性由后台 worker 推进。
        await self._maybe_enqueue_internal_index(user_internal_id, file_orm)
        try:
            await self._maybe_enqueue_file_understanding(user_internal_id, file_orm)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "file understanding enqueue failed (swallowed) | file=%s | err_type=%s | err=%s",
                file_orm.public_id,
                type(exc).__name__,
                str(exc)[:200],
            )

        # P0 收口:cache invalidation 必须严格在 commit 之后.
        try:
            from app.cache.domains.conversation_cache import (
                get_conversation_cache,
            )
            from app.cache.domains.library_cache import get_library_cache
            from app.db.sync import register_after_commit

            _uid = user_internal_id
            _conv_pid = conv_public_id

            async def _do_bump(_session):
                try:
                    await get_conversation_cache().bump_generation(_uid)
                    # 文件库 (Library) list 也依赖 library generation.
                    await get_library_cache().bump_generation(_uid)
                except Exception as cache_exc:  # noqa: BLE001
                    logger.warning(
                        "FileService.upload: cache bump_generation failed | "
                        "user_id=%s | conv=%s | %s",
                        _uid, _conv_pid, cache_exc,
                    )

            register_after_commit(self._session, _do_bump)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "FileService.upload: cache hook register failed | "
                "user_id=%s | conv=%s | %s",
                user_internal_id, conv_public_id, exc,
            )

        return self._to_summary(file_orm)

    async def _maybe_enqueue_file_understanding(
        self,
        user_internal_id: int,
        uploaded_file: UploadedFile,
    ) -> None:
        capability = FileProcessingCapabilityRegistry().for_extension(uploaded_file.file_ext)
        if not capability.can_semantic_profile:
            return

        async def _run() -> None:
            try:
                await asyncio.sleep(0)
                from app.db.session import AsyncSessionLocal
                from app.services.file_understanding_service import FileUnderstandingService

                async with AsyncSessionLocal() as session:
                    await FileUnderstandingService(
                        session,
                        context_llm_invoker=self._context_llm_invoker,
                    ).understand_file(
                        user_internal_id=user_internal_id,
                        file_public_id=uploaded_file.public_id,
                    )
                    await session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "file understanding background failed (swallowed) | file=%s | err_type=%s | err=%s",
                    uploaded_file.public_id,
                    type(exc).__name__,
                    str(exc)[:200],
                )

        asyncio.create_task(_run())

    async def _maybe_enqueue_internal_index(
        self,
        user_internal_id: int,
        uploaded_file,
    ) -> None:
        """CPS-01: 触发 Internal RAG 索引。

        调用 IndexDocumentService.submit_uploaded_file；
        捕获 DocumentIndexError（不支持类型/超限/未授权）记 warning，不抛。
        其他未知异常也吞掉：上传已成功，索引失败不应破坏主流程。

        关键不变量（§七）：
        Index failure
        ≠
        UploadedFile rollback

        上传文件是 Source Truth，索引是 Derived Index。
        即便本次 enqueue 失败，Upload 也必须 2xx 返回。
        """
        try:
            from app.context_engine.indexing.document_service import (
                DocumentIndexError,
                IndexDocumentService,
            )

            await IndexDocumentService(self._session).submit_uploaded_file(
                user_id=user_internal_id,
                file_public_id=uploaded_file.public_id,
            )
            logger.info(
                "CPS-01 索引 enqueue 成功 | file=%s",
                uploaded_file.public_id,
            )
        except DocumentIndexError as exc:
            logger.warning(
                "CPS-01 索引跳过（已吞掉，不阻塞上传）| file=%s | conv=%s | code=%s | detail=%s",
                uploaded_file.public_id,
                getattr(uploaded_file, "conversation_id", None),
                exc.code,
                exc.detail,
            )
        except Exception as exc:  # noqa: BLE001
            # 兜底任何异常（包括 SQLAlchemyError、NameError、IOError 等）
            # 必须吞掉，避免上传事务被回滚。
            logger.warning(
                "CPS-01 索引失败（已吞掉，不阻塞上传）| file=%s | conv=%s | err_type=%s | err_msg=%s",
                uploaded_file.public_id,
                getattr(uploaded_file, "conversation_id", None),
                type(exc).__name__,
                str(exc)[:200],
            )

    async def list_files(
        self, user_internal_id: int, conv_public_id: str
    ) -> tuple[list[dict], int]:
        conv = await self._conv_repo.get_by_public_id(conv_public_id)
        if not conv:
            return [], 0
        files = await self._file_repo.list_by_conversation(user_internal_id, conv.id)
        summaries = [self._to_summary(f) for f in files]
        return summaries, len(summaries)

    async def confirm_type(
        self,
        file_public_id: str,
        file_type: str,
        *,
        user_internal_id: int,
    ) -> dict:
        allowed = {"requirement_doc", "test_plan_template", "supplemental_doc", "unknown"}
        if file_type not in allowed:
            raise ValidationError("文件类型不合法")
        uploaded = await self._file_repo.get_any_by_public_id(file_public_id)
        if uploaded is None or uploaded.deleted_at is not None:
            raise NotFoundError("文件")
        if uploaded.user_id != user_internal_id:
            raise ForbiddenError()
        await self._file_repo.update_type(file_public_id, file_type)
        return {
            "file_id": file_public_id,
            "file_type": file_type,
            "upload_status": "confirmed",
        }

    async def delete(
        self,
        file_public_id: str,
        *,
        user_internal_id: int,
        conv_public_id: str | None = None,
    ) -> None:
        uploaded = await self._file_repo.get_any_by_public_id(file_public_id)
        if uploaded is None or uploaded.deleted_at is not None:
            raise NotFoundError("文件")
        if uploaded.user_id != user_internal_id:
            raise ForbiddenError()
        if conv_public_id:
            conv = await self._conv_repo.get_by_public_id(conv_public_id)
            if conv is None or conv.id != uploaded.conversation_id or conv.user_id != user_internal_id:
                raise ForbiddenError()
        now = utcnow()
        await self._file_repo.soft_delete(file_public_id, now)
        await ContextIndexDocumentRepository(self._session).invalidate_uploaded_file(
            user_id=user_internal_id,
            file_public_id=file_public_id,
            now=now,
        )
        try:
            await self._storage.delete_file(uploaded.storage_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "uploaded file OSS cleanup failed (swallowed) | file=%s | err_type=%s | err=%s",
                file_public_id,
                type(exc).__name__,
                str(exc)[:200],
            )

        # P0 收口:cache invalidation 必须严格在 commit 之后.
        try:
            from app.cache.domains.conversation_cache import (
                get_conversation_cache,
            )
            from app.cache.domains.library_cache import get_library_cache
            from app.db.sync import register_after_commit

            _uid = user_internal_id
            _fpid = file_public_id

            async def _do_bump(_session):
                try:
                    await get_conversation_cache().bump_generation(_uid)
                    await get_library_cache().bump_generation(_uid)
                except Exception as cache_exc:  # noqa: BLE001
                    logger.warning(
                        "FileService.delete: cache bump_generation failed | "
                        "user_id=%s | file=%s | %s",
                        _uid, _fpid, cache_exc,
                    )

            register_after_commit(self._session, _do_bump)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "FileService.delete: cache hook register failed | "
                "user_id=%s | file=%s | %s",
                user_internal_id, file_public_id, exc,
            )

    @staticmethod
    def _guess_type(ext: str) -> str:
        # Default to "unknown"; user confirms via confirm_type
        return "unknown"

    @staticmethod
    def _resolve_mime_type(file_name: str, content_type: str | None) -> str | None:
        if content_type and content_type != "application/octet-stream":
            return content_type
        guessed, _ = mimetypes.guess_type(file_name)
        return guessed

    @staticmethod
    def _to_summary(f: UploadedFile) -> dict:
        return {
            "id": f.public_id,
            "original_name": f.original_name,
            "file_ext": f.file_ext,
            "mime_type": f.mime_type,
            "file_size": f.file_size,
            "file_type": f.file_type,
            "upload_status": f.upload_status,
        }


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (文件上传/识别/删除核心服务):
#
#   链路 (上传):
#     前端 chat input → 用户上传 .docx / .txt / .pdf / .md
#       → api/v1/files.py upload endpoint
#         → FileService.save_upload(file_bytes, filename, content_type, user_id, conversation_id)
#           → local_storage.save_bytes(...) 落磁盘
#           → UploadedFile 行 + file_capability 行写库
#         → 返回 public_id 给前端,在 fileStore 里登记
#
#   链路 (类型识别 / confirm type):
#     当前由 confirm_type_endpoint / file_understanding_service 触发
#       → FileService.confirm_uploaded_file_type(public_id, file_type)
#         → 改 uploaded_file.file_type 字段
#         → (Phase 3 attachment_understanding) 也走这里
#
#   链路 (删除):
#     前端 chat 列表删除按钮 → api/v1/files.py delete endpoint
#       → FileService.delete_file(public_id, user_id)
#         → 检查权限 (owner = user_internal_id)
#         → local_storage.delete_bytes(...)
#         → UploadedFile.delete()
#
# 关键约束(供开发者速查):
#   - 服务始终使用 public_id(字符串)而非内部 int id;权限校验必须在
#     commit 前完成(SQLAlchemy 异步 session);
#   - file_capability_registry 单例,用于类型确认路由;
#   - 文件系统存储路径由 local_storage 决定,不要在本模块直接拼路径;
#   - 删除会级联到文件相关的 message_attachment 反向引用,需要在事务里一起处理。
