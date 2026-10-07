from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.core.exceptions import ForbiddenError, ValidationError
from app.models.agent_task import AgentTask
from app.models.context_engine import ContextIndexChunk, ContextIndexDocument, ContextIndexJob
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.uploaded_file import UploadedFile
from app.models.user import User
from app.services.file_service import FileService


def _now() -> datetime:
    return datetime(2026, 8, 9, 12, 0, 0)


async def _seed_user_conversation(session, *, user_id: int = 1, conv_id: int = 1):
    existing_user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if existing_user is None:
        session.add(
            User(
                id=user_id,
                public_id=f"user_{user_id}",
                username=f"user{user_id}",
                password_hash="hash",
                created_at=_now(),
                updated_at=_now(),
            )
        )
    session.add(
        Conversation(
            id=conv_id,
            public_id=f"conv_{conv_id}",
            user_id=user_id,
            title="Test",
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await session.flush()


async def _seed_file(
    session,
    *,
    file_id: int,
    public_id: str,
    user_id: int,
    conv_id: int,
    file_type: str = "unknown",
    deleted_at: datetime | None = None,
):
    f = UploadedFile(
        id=file_id,
        public_id=public_id,
        user_id=user_id,
        conversation_id=conv_id,
        original_name=f"{public_id}.docx",
        stored_name=f"{public_id}.docx",
        file_ext="docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=128,
        file_hash=None,
        file_type=file_type,
        upload_status="uploaded",
        storage_type="local",
        storage_path=f"data/uploads/{public_id}.docx",
        created_at=_now(),
        updated_at=_now(),
        deleted_at=deleted_at,
    )
    session.add(f)
    await session.flush()
    return f


async def test_message_attachment_repository_preserves_positions(sqlite_session_factory):
    from app.repositories.attachment_understanding_repository import MessageAttachmentRepository

    sf = sqlite_session_factory
    async with sf() as session:
        await _seed_user_conversation(session)
        files = [
            await _seed_file(session, file_id=10, public_id="file_a", user_id=1, conv_id=1),
            await _seed_file(session, file_id=20, public_id="file_b", user_id=1, conv_id=1),
            await _seed_file(session, file_id=30, public_id="file_c", user_id=1, conv_id=1),
        ]
        msg = Message(
            id=100,
            public_id="msg_1",
            user_id=1,
            conversation_id=1,
            role="user",
            message_type="user_text",
            content="第一个是需求，第二个是模板",
            payload_json={"attached_file_ids": ["file_a", "file_b", "file_c"]},
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(msg)
        await session.flush()

        repo = MessageAttachmentRepository(session)
        await repo.create_ordered_for_message(message=msg, files=files)
        attachments = await repo.list_for_message(msg.id, user_id=1)

        assert [a.file_id for a in attachments] == [10, 20, 30]
        assert [a.position for a in attachments] == [0, 1, 2]


async def test_message_attachment_repository_rejects_cross_conversation_file(sqlite_session_factory):
    from app.repositories.attachment_understanding_repository import MessageAttachmentRepository

    sf = sqlite_session_factory
    async with sf() as session:
        await _seed_user_conversation(session, user_id=1, conv_id=1)
        await _seed_user_conversation(session, user_id=1, conv_id=2)
        cross_file = await _seed_file(session, file_id=20, public_id="file_cross", user_id=1, conv_id=2)
        msg = Message(
            id=100,
            public_id="msg_1",
            user_id=1,
            conversation_id=1,
            role="user",
            message_type="user_text",
            content="attach",
            payload_json={"attached_file_ids": ["file_cross"]},
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(msg)
        await session.flush()

        repo = MessageAttachmentRepository(session)
        with pytest.raises(ForbiddenError):
            await repo.create_ordered_for_message(message=msg, files=[cross_file])


async def test_task_file_binding_repository_persists_legacy_projection(sqlite_session_factory):
    from app.repositories.attachment_understanding_repository import TaskFileBindingRepository

    sf = sqlite_session_factory
    async with sf() as session:
        await _seed_user_conversation(session)
        await _seed_file(session, file_id=10, public_id="file_req", user_id=1, conv_id=1)
        await _seed_file(session, file_id=20, public_id="file_tpl", user_id=1, conv_id=1)
        task = AgentTask(
            id=100,
            public_id="task_1",
            user_id=1,
            conversation_id=1,
            task_type="test_plan_generation",
            requirement_file_id=10,
            template_file_id=20,
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(task)
        await session.flush()

        repo = TaskFileBindingRepository(session)
        await repo.create_legacy_projection(task)
        bindings = await repo.list_for_task(task.id, user_id=1)

        assert [(b.file_id, b.binding_role, b.position, b.is_primary) for b in bindings] == [
            (10, "requirement_source", 0, True),
            (20, "output_template", 0, True),
        ]
        assert {b.binding_source for b in bindings} == {"legacy"}


async def test_backfill_service_restores_message_task_and_legacy_profiles(sqlite_session_factory):
    from app.models.attachment_understanding import FileSemanticProfile, MessageAttachment, TaskFileBinding
    from app.services.attachment_backfill_service import AttachmentBackfillService

    sf = sqlite_session_factory
    async with sf() as session:
        await _seed_user_conversation(session)
        await _seed_file(
            session,
            file_id=10,
            public_id="file_req",
            user_id=1,
            conv_id=1,
            file_type="requirement_doc",
        )
        await _seed_file(
            session,
            file_id=20,
            public_id="file_tpl",
            user_id=1,
            conv_id=1,
            file_type="test_plan_template",
        )
        msg = Message(
            id=100,
            public_id="msg_1",
            user_id=1,
            conversation_id=1,
            role="user",
            message_type="user_text",
            content="generate",
            payload_json={"attached_file_ids": ["file_tpl", "file_req"]},
            created_at=_now(),
            updated_at=_now(),
        )
        task = AgentTask(
            id=200,
            public_id="task_1",
            user_id=1,
            conversation_id=1,
            task_type="test_plan_generation",
            requirement_file_id=10,
            template_file_id=20,
            created_at=_now(),
            updated_at=_now(),
        )
        session.add_all([msg, task])
        await session.flush()

        service = AttachmentBackfillService(session)
        assert await service.backfill_message_attachments() == 2
        assert await service.backfill_task_file_bindings() == 2
        assert await service.seed_legacy_file_semantic_profiles() == 2

        message_attachments = (
            await session.execute(
                select(MessageAttachment).order_by(MessageAttachment.position.asc())
            )
        ).scalars().all()
        task_bindings = (
            await session.execute(select(TaskFileBinding).order_by(TaskFileBinding.id.asc()))
        ).scalars().all()
        profiles = (
            await session.execute(select(FileSemanticProfile).order_by(FileSemanticProfile.file_id.asc()))
        ).scalars().all()

        assert [row.file_id for row in message_attachments] == [20, 10]
        assert {(row.file_id, row.binding_role) for row in task_bindings} == {
            (10, "requirement_source"),
            (20, "output_template"),
        }
        assert [(row.file_id, row.document_kind) for row in profiles] == [
            (10, "requirements_specification"),
            (20, "test_plan_template"),
        ]


async def test_confirm_type_rejects_invalid_enum(sqlite_session_factory):
    sf = sqlite_session_factory
    async with sf() as session:
        await _seed_user_conversation(session)
        await _seed_file(session, file_id=10, public_id="file_1", user_id=1, conv_id=1)

        with pytest.raises(ValidationError):
            await FileService(session).confirm_type(
                "file_1",
                "malicious_role",
                user_internal_id=1,
            )


async def test_confirm_type_rejects_cross_user_file(sqlite_session_factory):
    sf = sqlite_session_factory
    async with sf() as session:
        await _seed_user_conversation(session, user_id=1, conv_id=1)
        await _seed_user_conversation(session, user_id=2, conv_id=2)
        await _seed_file(session, file_id=20, public_id="file_other", user_id=2, conv_id=2)

        with pytest.raises(ForbiddenError):
            await FileService(session).confirm_type(
                "file_other",
                "requirement_doc",
                user_internal_id=1,
            )


async def test_delete_file_invalidates_uploaded_file_rag_rows(sqlite_session_factory):
    sf = sqlite_session_factory
    async with sf() as session:
        await _seed_user_conversation(session)
        await _seed_file(session, file_id=10, public_id="file_1", user_id=1, conv_id=1)
        doc = ContextIndexDocument(
            id=100,
            public_id="idx_doc_1",
            user_id=1,
            workspace_key="conversation:conv_1",
            source_type="uploaded_file",
            source_public_id="file_1",
            source_version="1",
            source_digest="a" * 64,
            title="file_1.docx",
            status="indexed",
            chunk_policy_key="recursive_char:v1",
            chunk_policy_version="v1",
            lexical_index_status="ready",
            vector_index_status="ready",
            idempotency_key="idx:file_1",
            created_at=_now(),
            updated_at=_now(),
        )
        chunk = ContextIndexChunk(
            id=200,
            public_id="idx_chunk_1",
            document_id=100,
            user_id=1,
            chunk_index=0,
            content="canary",
            content_hash="b" * 64,
            char_count=6,
            status="active",
            created_at=_now(),
            updated_at=_now(),
        )
        session.add_all([doc, chunk])
        await session.flush()

        await FileService(session).delete(
            "file_1",
            user_internal_id=1,
            conv_public_id="conv_1",
        )
        await session.refresh(doc)
        await session.refresh(chunk)

        assert doc.status == "deleted"
        assert doc.deleted_at is not None
        assert chunk.status == "deleted"
        assert chunk.deleted_at is not None
        jobs = (
            await session.execute(
                select(ContextIndexJob).where(ContextIndexJob.document_id == doc.id)
            )
        ).scalars().all()
        assert [job.operation for job in jobs] == ["delete_external"]


def test_file_capability_registry_reports_current_truth():
    from app.services.file_capability_registry import FileProcessingCapabilityRegistry

    registry = FileProcessingCapabilityRegistry()

    assert registry.for_extension("docx").can_parse is True
    assert registry.for_extension(".docx").can_index is True
    assert registry.for_extension("png").can_vision is True
    assert registry.for_extension("png").can_semantic_profile is False
    assert registry.for_extension("pdf").can_index is False
