"""File repository."""

from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.models.uploaded_file import UploadedFile
from app.repositories.base import BaseRepository, ensure_model_id


class FileRepository(BaseRepository[UploadedFile]):
    model = UploadedFile

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_by_conversation(self, user_id: int, conv_internal_id: int, limit: int = 50) -> list[UploadedFile]:
        result = await self.session.execute(
            select(UploadedFile)
            .where(
                UploadedFile.user_id == user_id,
                UploadedFile.conversation_id == conv_internal_id,
                UploadedFile.deleted_at.is_(None),
            )
            .order_by(UploadedFile.created_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_by_user(self, user_id: int, limit: int = 200) -> list[UploadedFile]:
        """Return the user's visible uploads for a library-style aggregate view."""
        result = await self.session.execute(
            select(UploadedFile).options(
                load_only(
                    UploadedFile.id,
                    UploadedFile.public_id,
                    UploadedFile.original_name,
                    UploadedFile.mime_type,
                    UploadedFile.file_ext,
                    UploadedFile.file_size,
                    UploadedFile.created_at,
                    UploadedFile.updated_at,
                    UploadedFile.deleted_at,
                )
            )
            .where(
                UploadedFile.user_id == user_id,
                UploadedFile.deleted_at.is_(None),
            )
            .order_by(UploadedFile.updated_at.desc(), UploadedFile.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_deleted_by_user(self, user_id: int, limit: int = 200) -> list[UploadedFile]:
        result = await self.session.execute(
            select(UploadedFile).options(
                load_only(
                    UploadedFile.id,
                    UploadedFile.public_id,
                    UploadedFile.original_name,
                    UploadedFile.mime_type,
                    UploadedFile.file_ext,
                    UploadedFile.file_size,
                    UploadedFile.created_at,
                    UploadedFile.updated_at,
                    UploadedFile.deleted_at,
                )
            )
            .where(UploadedFile.user_id == user_id, UploadedFile.deleted_at.is_not(None))
            .order_by(UploadedFile.deleted_at.desc(), UploadedFile.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_by_conversation(self, conversation_id: int) -> int:
        """Count all non-deleted uploaded files in a conversation."""
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count())
            .select_from(UploadedFile)
            .where(
                UploadedFile.conversation_id == conversation_id,
                UploadedFile.deleted_at.is_(None),
            )
        )
        return result.scalar() or 0

    async def count_group_by_conversation(
        self, user_id: int, conversation_ids: list[int]
    ) -> dict[int, int]:
        """Batch count files grouped by conversation_id (Phase 0 — list view).

        Companion to ``MessageRepository.count_group_by_conversation``.
        Single SELECT + GROUP BY; only non-deleted files counted.
        """
        from sqlalchemy import func

        if not conversation_ids:
            return {}
        result = await self.session.execute(
            select(UploadedFile.conversation_id, func.count())
            .where(
                UploadedFile.user_id == user_id,
                UploadedFile.conversation_id.in_(conversation_ids),
                UploadedFile.deleted_at.is_(None),
            )
            .group_by(UploadedFile.conversation_id)
        )
        return {row[0]: int(row[1]) for row in result.all()}

    async def get_by_public_id(self, user_id: int, public_id: str) -> UploadedFile | None:
        result = await self.session.execute(
            select(UploadedFile).where(
                UploadedFile.user_id == user_id,
                UploadedFile.public_id == public_id,
                UploadedFile.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_any_by_public_id(self, public_id: str) -> UploadedFile | None:
        result = await self.session.execute(
            select(UploadedFile).where(UploadedFile.public_id == public_id)
        )
        return result.scalar_one_or_none()

    async def create(self, f: UploadedFile) -> UploadedFile:
        await ensure_model_id(self.session, UploadedFile, f)
        self.session.add(f)
        await self.session.flush()
        return f

    async def update_type(self, public_id: str, file_type: str) -> None:
        await self.session.execute(
            update(UploadedFile)
            .where(UploadedFile.public_id == public_id)
            .values(file_type=file_type, upload_status="confirmed")
        )

    async def soft_delete(self, public_id: str, now: object) -> None:
        await self.session.execute(
            update(UploadedFile)
            .where(UploadedFile.public_id == public_id)
            .values(deleted_at=now)
        )

    async def rename(self, public_id: str, name: str, now: object) -> None:
        await self.session.execute(
            update(UploadedFile)
            .where(UploadedFile.public_id == public_id)
            .values(original_name=name, updated_at=now)
        )

    async def restore(self, public_id: str, now: object) -> None:
        await self.session.execute(
            update(UploadedFile)
            .where(UploadedFile.public_id == public_id)
            .values(deleted_at=None, updated_at=now)
        )

    async def hard_delete(self, public_id: str) -> None:
        await self.session.execute(delete(UploadedFile).where(UploadedFile.public_id == public_id))


# 模块定位:File 仓储(UploadedFile ORM 行)
#
# 链路:
#   FileService.save_upload → upsert UploadedFile 行
#   FileUnderstandingService.understand_uploaded_file → 更新 capability_identified
#
# 关键约束:
#   - storage_path **不**对外暴露;
#   - file_type 字段在 confirm 前可为 None;
#   - delete 必须级联 storage 磁盘清理(本仓不负责)。
