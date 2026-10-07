"""Backfill helpers for attachment understanding PHASE-1."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.json_utils import normalize_json_object
from app.models.agent_task import AgentTask
from app.models.message import Message
from app.models.uploaded_file import UploadedFile
from app.repositories.attachment_understanding_repository import (
    FileSemanticProfileRepository,
    MessageAttachmentRepository,
    TaskFileBindingRepository,
)

logger = logging.getLogger(__name__)


class AttachmentBackfillService:
    """Idempotent migration-period backfill service."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._message_attachment_repo = MessageAttachmentRepository(session)
        self._task_binding_repo = TaskFileBindingRepository(session)
        self._profile_repo = FileSemanticProfileRepository(session)

    async def backfill_message_attachments(self, *, limit: int = 1000) -> int:
        result = await self._session.execute(
            select(Message).where(Message.deleted_at.is_(None)).limit(limit)
        )
        messages = list(result.scalars().all())
        count = 0
        for message in messages:
            payload = normalize_json_object(message.payload_json)
            ids = payload.get("attached_file_ids", [])
            if not isinstance(ids, list) or not ids:
                continue
            files_result = await self._session.execute(
                select(UploadedFile).where(
                    UploadedFile.public_id.in_([str(file_id) for file_id in ids]),
                    UploadedFile.user_id == message.user_id,
                    UploadedFile.conversation_id == message.conversation_id,
                    UploadedFile.deleted_at.is_(None),
                )
            )
            by_public_id = {file.public_id: file for file in files_result.scalars().all()}
            ordered_files = [
                by_public_id[file_public_id]
                for file_public_id in [str(file_id) for file_id in ids]
                if file_public_id in by_public_id
            ]
            if len(ordered_files) != len(ids):
                logger.warning(
                    "Attachment backfill skipped invalid message attachment refs | message=%s",
                    message.public_id,
                )
            if ordered_files:
                await self._message_attachment_repo.create_ordered_for_message(
                    message=message,
                    files=ordered_files,
                )
                count += len(ordered_files)
        return count

    async def backfill_task_file_bindings(self, *, limit: int = 1000) -> int:
        result = await self._session.execute(
            select(AgentTask).where(AgentTask.deleted_at.is_(None)).limit(limit)
        )
        tasks = list(result.scalars().all())
        count = 0
        for task in tasks:
            count += len(await self._task_binding_repo.create_legacy_projection(task))
        return count

    async def seed_legacy_file_semantic_profiles(self, *, limit: int = 1000) -> int:
        result = await self._session.execute(
            select(UploadedFile)
            .where(
                UploadedFile.deleted_at.is_(None),
                UploadedFile.file_type.in_(["requirement_doc", "test_plan_template"]),
            )
            .limit(limit)
        )
        count = 0
        for uploaded_file in result.scalars().all():
            document_kind = (
                "requirements_specification"
                if uploaded_file.file_type == "requirement_doc"
                else "test_plan_template"
            )
            await self._profile_repo.seed_legacy_profile(
                uploaded_file=uploaded_file,
                document_kind=document_kind,
            )
            count += 1
        return count



# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (Attachment Understanding PHASE-1 回填):
#
#   链路 (PHASE-1 数据回填,一次性任务):
#     部署后或升级后,旧上传文件没有 attachment_understanding 行
#       → 运维/手动触发 attachment_backfill_service.run_backfill(user_id=可选)
#         → 拉取 uploaded_file 列表,逐文件调 ImageUnderstandingOrchestrator
#           把图片理解结果写入 attachment_understanding 表
#       → 与常规 chat 上传路径不同,本服务跳过 OCR/Vision Pipeline 的
#         同步调用(改异步批量)
#
# 关键约束(供开发者速查):
#   - 仅 PHASE-1 一次性使用;完成后不再调用,避免重复跑;
#   - 失败文件写入 attachment_understanding_attempts 表,允许重试;
#   - 不阻塞用户实时上传路径(后者走 file_understanding_service);
#   - 大批量时建议分批 (batch_size),避免一次性把 DB / 队列打满。
