"""Repositories for attachment understanding foundation tables."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError
from app.models.agent_task import AgentTask
from app.models.attachment_understanding import FileSemanticProfile, MessageAttachment, TaskFileBinding
from app.models.message import Message
from app.models.uploaded_file import UploadedFile
from app.repositories.base import BaseRepository, ensure_model_id
from app.utils.ids import generate_public_id


class MessageAttachmentRepository(BaseRepository[MessageAttachment]):
    model = MessageAttachment

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def create_ordered_for_message(
        self,
        *,
        message: Message,
        files: list[UploadedFile],
    ) -> list[MessageAttachment]:
        attachments: list[MessageAttachment] = []
        for position, uploaded_file in enumerate(files):
            if uploaded_file.user_id != message.user_id:
                raise ForbiddenError()
            if uploaded_file.conversation_id != message.conversation_id:
                raise ForbiddenError()
            if uploaded_file.deleted_at is not None:
                raise ForbiddenError()
            existing = await self.session.execute(
                select(MessageAttachment).where(
                    MessageAttachment.message_id == message.id,
                    MessageAttachment.file_id == uploaded_file.id,
                )
            )
            row = existing.scalar_one_or_none()
            if row is None:
                row = MessageAttachment(
                    message_id=message.id,
                    file_id=uploaded_file.id,
                    position=position,
                )
                await ensure_model_id(self.session, MessageAttachment, row)
                self.session.add(row)
            else:
                row.position = position
            attachments.append(row)
        await self.session.flush()
        return attachments

    async def list_for_message(self, message_id: int, *, user_id: int) -> list[MessageAttachment]:
        stmt = (
            select(MessageAttachment)
            .join(Message, Message.id == MessageAttachment.message_id)
            .where(MessageAttachment.message_id == message_id, Message.user_id == user_id)
            .order_by(MessageAttachment.position.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class TaskFileBindingRepository(BaseRepository[TaskFileBinding]):
    model = TaskFileBinding

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def create_binding(
        self,
        *,
        task: AgentTask,
        file_id: int,
        binding_role: str,
        position: int,
        is_primary: bool,
        binding_source: str,
        confidence: float,
        metadata_json: dict | None = None,
    ) -> TaskFileBinding:
        existing = await self.session.execute(
            select(TaskFileBinding).where(
                TaskFileBinding.task_id == task.id,
                TaskFileBinding.file_id == file_id,
                TaskFileBinding.binding_role == binding_role,
            )
        )
        row = existing.scalar_one_or_none()
        if row is None:
            row = TaskFileBinding(
                task_id=task.id,
                file_id=file_id,
                binding_role=binding_role,
                position=position,
                is_primary=is_primary,
                binding_source=binding_source,
                confidence=Decimal(str(confidence)),
                metadata_json=metadata_json,
            )
            await ensure_model_id(self.session, TaskFileBinding, row)
            self.session.add(row)
        else:
            row.position = position
            row.is_primary = is_primary
            row.binding_source = binding_source
            row.confidence = Decimal(str(confidence))
            row.metadata_json = metadata_json
        await self.session.flush()
        return row

    async def create_legacy_projection(self, task: AgentTask) -> list[TaskFileBinding]:
        bindings: list[TaskFileBinding] = []
        if task.requirement_file_id is not None:
            bindings.append(
                await self.create_binding(
                    task=task,
                    file_id=int(task.requirement_file_id),
                    binding_role="requirement_source",
                    position=0,
                    is_primary=True,
                    binding_source="legacy",
                    confidence=1.0,
                )
            )
        if task.template_file_id is not None:
            bindings.append(
                await self.create_binding(
                    task=task,
                    file_id=int(task.template_file_id),
                    binding_role="output_template",
                    position=0,
                    is_primary=True,
                    binding_source="legacy",
                    confidence=1.0,
                )
            )
        return bindings

    async def persist_resolved_bindings(
        self,
        *,
        task: AgentTask,
        bindings: list,
    ) -> list[TaskFileBinding]:
        rows: list[TaskFileBinding] = []
        primary_requirement_id: int | None = None
        primary_template_id: int | None = None
        for binding in bindings:
            row = await self.create_binding(
                task=task,
                file_id=int(binding.file_id),
                binding_role=str(binding.binding_role),
                position=int(binding.position),
                is_primary=bool(binding.is_primary),
                binding_source=str(binding.binding_source),
                confidence=float(binding.confidence),
                metadata_json=getattr(binding, "metadata_json", None),
            )
            rows.append(row)
            if binding.binding_role == "requirement_source" and binding.is_primary:
                primary_requirement_id = int(binding.file_id)
            if binding.binding_role == "output_template" and binding.is_primary:
                primary_template_id = int(binding.file_id)

        task.requirement_file_id = primary_requirement_id
        task.template_file_id = primary_template_id
        await self.session.execute(
            update(AgentTask)
            .where(AgentTask.id == task.id)
            .values(
                requirement_file_id=primary_requirement_id,
                template_file_id=primary_template_id,
            )
        )
        await self.session.flush()
        return rows

    async def list_for_task(self, task_id: int, *, user_id: int) -> list[TaskFileBinding]:
        stmt = (
            select(TaskFileBinding)
            .join(AgentTask, AgentTask.id == TaskFileBinding.task_id)
            .where(TaskFileBinding.task_id == task_id, AgentTask.user_id == user_id)
            .order_by(
                case(
                    (TaskFileBinding.binding_role == "requirement_source", 0),
                    (TaskFileBinding.binding_role == "output_template", 1),
                    else_=2,
                ),
                TaskFileBinding.position.asc(),
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class FileSemanticProfileRepository(BaseRepository[FileSemanticProfile]):
    model = FileSemanticProfile

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_file_id(self, file_id: int) -> FileSemanticProfile | None:
        result = await self.session.execute(
            select(FileSemanticProfile).where(
                FileSemanticProfile.file_id == file_id,
                FileSemanticProfile.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def safe_index_metadata_for_file(
        self,
        *,
        uploaded_file: UploadedFile,
    ) -> dict[str, Any]:
        profile = await self.get_by_file_id(uploaded_file.id)
        return self.safe_index_metadata(uploaded_file=uploaded_file, profile=profile)

    @staticmethod
    def safe_index_metadata(
        *,
        uploaded_file: UploadedFile,
        profile: FileSemanticProfile | None,
    ) -> dict[str, Any]:
        if profile is None or profile.status != "ready":
            return {}
        confidence = profile.confidence
        if isinstance(confidence, Decimal):
            confidence_value = float(confidence)
        elif confidence is None:
            confidence_value = None
        else:
            confidence_value = float(confidence)
        return {
            "file_public_id": uploaded_file.public_id,
            "document_kind": profile.document_kind or "unknown",
            "semantic_labels": list(profile.semantic_labels_json or []),
            "profile_confidence": confidence_value,
            "profile_version": profile.classifier_version or "",
        }

    async def upsert_profile(
        self,
        *,
        uploaded_file: UploadedFile,
        status: str,
        document_kind: str = "unknown",
        summary: str | None = None,
        semantic_labels: list | None = None,
        possible_usages: list | None = None,
        characteristics: dict | None = None,
        confidence: float | None = None,
        classifier_version: str | None = None,
        source_hash: str | None = None,
        error_code: str | None = None,
    ) -> FileSemanticProfile:
        profile = await self.get_by_file_id(uploaded_file.id)
        if profile is None:
            profile = FileSemanticProfile(
                public_id=generate_public_id("fsp"),
                file_id=uploaded_file.id,
                user_id=uploaded_file.user_id,
                conversation_id=uploaded_file.conversation_id,
            )
            await ensure_model_id(self.session, FileSemanticProfile, profile)
            self.session.add(profile)
        profile.status = status
        profile.document_kind = document_kind
        profile.summary = summary
        profile.semantic_labels_json = semantic_labels or []
        profile.possible_usages_json = possible_usages or []
        profile.characteristics_json = characteristics or {}
        profile.confidence = Decimal(str(confidence)) if confidence is not None else None
        profile.classifier_version = classifier_version
        profile.source_hash = source_hash
        profile.error_code = error_code
        await self.session.flush()
        return profile

    async def seed_legacy_profile(
        self,
        *,
        uploaded_file: UploadedFile,
        document_kind: str,
        classifier_version: str = "legacy:file_type:v1",
    ) -> FileSemanticProfile:
        result = await self.session.execute(
            select(FileSemanticProfile).where(FileSemanticProfile.file_id == uploaded_file.id)
        )
        profile = result.scalar_one_or_none()
        if profile is None:
            profile = FileSemanticProfile(
                public_id=generate_public_id("fsp"),
                file_id=uploaded_file.id,
                user_id=uploaded_file.user_id,
                conversation_id=uploaded_file.conversation_id,
                status="ready",
                document_kind=document_kind,
                confidence=Decimal("1.0"),
                classifier_version=classifier_version,
                possible_usages_json=[],
                semantic_labels_json=[],
                characteristics_json={"seed_source": "uploaded_file.file_type"},
            )
            await ensure_model_id(self.session, FileSemanticProfile, profile)
            self.session.add(profile)
        await self.session.flush()
        return profile


# 模块定位:AttachmentUnderstanding 仓储(PHASE-1 图像理解)
#
# 链路:
#   FileUnderstandingService.understand_uploaded_file → upsert
#
# 关键约束:
#   - attachment_id UNIQUE(同 attachment 多次理解可叠加版本);
#   - status='failed' 时 caption 不写,只 error_message;
#   - 大小限制 caption ≤ 2KB / tags ≤ 16 / ocr ≤ 8KB。
