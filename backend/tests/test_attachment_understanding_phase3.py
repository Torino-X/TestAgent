from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.models.agent_task import AgentTask
from app.models.attachment_understanding import FileSemanticProfile, TaskFileBinding
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.uploaded_file import UploadedFile
from app.models.user import User


def _now() -> datetime:
    return datetime(2026, 8, 9, 12, 0, 0)


async def _seed_user_conversation(session, *, user_id: int = 1, conv_id: int = 1):
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
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
    name: str,
    user_id: int = 1,
    conv_id: int = 1,
    file_ext: str = "docx",
    file_type: str = "unknown",
):
    uploaded = UploadedFile(
        id=file_id,
        public_id=public_id,
        user_id=user_id,
        conversation_id=conv_id,
        original_name=name,
        stored_name=name,
        file_ext=file_ext,
        mime_type="application/octet-stream",
        file_size=128,
        file_hash=f"hash-{public_id}",
        file_type=file_type,
        upload_status="uploaded",
        storage_type="local",
        storage_path=f"uploads/{name}",
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(uploaded)
    await session.flush()
    return uploaded


async def _seed_profile(
    session,
    *,
    uploaded: UploadedFile,
    document_kind: str,
    possible_usages: list[str],
    confidence: float = 0.92,
):
    profile = FileSemanticProfile(
        id=1000 + uploaded.id,
        public_id=f"fsp_{uploaded.id}",
        file_id=uploaded.id,
        user_id=uploaded.user_id,
        conversation_id=uploaded.conversation_id,
        status="ready",
        document_kind=document_kind,
        summary=f"{document_kind} summary",
        semantic_labels_json=[],
        possible_usages_json=possible_usages,
        characteristics_json={},
        confidence=confidence,
        classifier_version="test",
        source_hash=f"hash-{uploaded.public_id}",
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(profile)
    await session.flush()
    return profile


def test_task_attachment_schema_registry_contracts():
    from app.services.task_attachment_schema_registry import TaskAttachmentSchemaRegistry

    registry = TaskAttachmentSchemaRegistry()
    test_plan = registry.for_task_type("test_plan_generation")
    normal_chat = registry.for_task_type("normal_chat")

    assert test_plan.required_role_names == ["requirement_source", "output_template"]
    assert test_plan.role("requirement_source").min_count == 1
    assert test_plan.role("requirement_source").max_count is None
    assert test_plan.role("output_template").min_count == 1
    assert test_plan.role("output_template").max_count == 1
    assert test_plan.role("reference_material").required is False
    assert normal_chat.required_role_names == []
    assert registry.is_compatible("output_template", "png") is False
    assert registry.is_compatible("output_template", "docx") is True


async def test_resolver_binds_explicit_ordinals(sqlite_session_factory):
    from app.services.task_attachment_resolver import TaskAttachmentResolver

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        req = await _seed_file(session, file_id=10, public_id="file_a", name="A.docx")
        tpl = await _seed_file(session, file_id=20, public_id="file_b", name="B.docx")
        message = Message(
            id=100,
            public_id="msg_1",
            user_id=1,
            conversation_id=1,
            role="user",
            message_type="user_text",
            content="第一个是需求，第二个是模板",
            payload_json={"attached_file_ids": ["file_a", "file_b"]},
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(message)
        await session.flush()

        result = await TaskAttachmentResolver(session).resolve(
            task_type="test_plan_generation",
            user_message=message.content,
            ordered_files=[req, tpl],
            conversation_files=[req, tpl],
        )

        assert result.status == "RESOLVED"
        assert [(b.file_id, b.binding_role, b.binding_source) for b in result.bindings] == [
            (10, "requirement_source", "user_explicit"),
            (20, "output_template", "user_explicit"),
        ]


async def test_resolver_binds_exact_filenames(sqlite_session_factory):
    from app.services.task_attachment_resolver import TaskAttachmentResolver

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        req = await _seed_file(session, file_id=10, public_id="file_req", name="alpha.docx")
        tpl = await _seed_file(session, file_id=20, public_id="file_tpl", name="beta.docx")

        result = await TaskAttachmentResolver(session).resolve(
            task_type="test_plan_generation",
            user_message="用《alpha.docx》作为需求，用《beta.docx》作为模板",
            ordered_files=[tpl, req],
            conversation_files=[req, tpl],
        )

        assert result.status == "RESOLVED"
        assert [(b.file_id, b.binding_role) for b in result.bindings] == [
            (10, "requirement_source"),
            (20, "output_template"),
        ]


async def test_resolver_automatically_binds_unique_semantic_profiles(sqlite_session_factory):
    from app.services.task_attachment_resolver import TaskAttachmentResolver

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        req = await _seed_file(session, file_id=10, public_id="file_req", name="notes.md", file_ext="md")
        tpl = await _seed_file(session, file_id=20, public_id="file_tpl", name="layout.docx")
        await _seed_profile(
            session,
            uploaded=req,
            document_kind="requirements_specification",
            possible_usages=["requirement_source"],
        )
        await _seed_profile(
            session,
            uploaded=tpl,
            document_kind="test_plan_template",
            possible_usages=["output_template"],
        )

        result = await TaskAttachmentResolver(session).resolve(
            task_type="test_plan_generation",
            user_message="生成测试方案",
            ordered_files=[req, tpl],
            conversation_files=[req, tpl],
        )

        assert result.status == "RESOLVED"
        assert result.legacy_requirement_file_id == 10
        assert result.legacy_template_file_id == 20
        assert {b.binding_source for b in result.bindings} == {"automatic"}


async def test_resolver_binds_current_prd_and_template_names_without_profiles(sqlite_session_factory):
    from app.services.task_attachment_resolver import TaskAttachmentResolver

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        req = await _seed_file(
            session,
            file_id=10,
            public_id="file_req",
            name="smart-campus-PRD.docx",
            file_type="unknown",
        )
        tpl = await _seed_file(
            session,
            file_id=20,
            public_id="file_tpl",
            name="PlanWise-QA-template.docx",
            file_type="unknown",
        )

        result = await TaskAttachmentResolver(session).resolve(
            task_type="test_plan_generation",
            user_message="generate a test plan from this PRD and template",
            ordered_files=[req, tpl],
            conversation_files=[req, tpl],
        )

        assert result.status == "RESOLVED"
        assert [(b.file_id, b.binding_role) for b in result.bindings] == [
            (10, "requirement_source"),
            (20, "output_template"),
        ]
        assert {b.binding_source for b in result.bindings} == {"current_message_pair"}


async def test_resolver_double_template_requires_clarification(sqlite_session_factory):
    from app.services.task_attachment_resolver import TaskAttachmentResolver

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        req = await _seed_file(session, file_id=10, public_id="file_req", name="req.docx")
        tpl_a = await _seed_file(session, file_id=20, public_id="file_tpl_a", name="a.docx")
        tpl_b = await _seed_file(session, file_id=30, public_id="file_tpl_b", name="b.docx")
        await _seed_profile(session, uploaded=req, document_kind="requirements_specification", possible_usages=["requirement_source"])
        await _seed_profile(session, uploaded=tpl_a, document_kind="test_plan_template", possible_usages=["output_template"])
        await _seed_profile(session, uploaded=tpl_b, document_kind="test_plan_template", possible_usages=["output_template"])

        result = await TaskAttachmentResolver(session).resolve(
            task_type="test_plan_generation",
            user_message="生成测试方案",
            ordered_files=[req, tpl_a, tpl_b],
            conversation_files=[req, tpl_a, tpl_b],
        )

        assert result.status == "CLARIFICATION_REQUIRED"
        assert result.reason == "ambiguous_required_role"
        assert result.ambiguous_role == "output_template"


async def test_optional_reference_ambiguity_does_not_block(sqlite_session_factory):
    from app.services.task_attachment_resolver import TaskAttachmentResolver

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        req = await _seed_file(session, file_id=10, public_id="file_req", name="req.docx")
        tpl = await _seed_file(session, file_id=20, public_id="file_tpl", name="tpl.docx")
        ref_a = await _seed_file(session, file_id=30, public_id="file_ref_a", name="ref-a.md", file_ext="md")
        ref_b = await _seed_file(session, file_id=40, public_id="file_ref_b", name="ref-b.md", file_ext="md")
        await _seed_profile(session, uploaded=req, document_kind="requirements_specification", possible_usages=["requirement_source"])
        await _seed_profile(session, uploaded=tpl, document_kind="test_plan_template", possible_usages=["output_template"])
        await _seed_profile(session, uploaded=ref_a, document_kind="supplemental_reference", possible_usages=["reference_material"])
        await _seed_profile(session, uploaded=ref_b, document_kind="supplemental_reference", possible_usages=["reference_material"])

        result = await TaskAttachmentResolver(session).resolve(
            task_type="test_plan_generation",
            user_message="生成测试方案",
            ordered_files=[req, tpl, ref_a, ref_b],
            conversation_files=[req, tpl, ref_a, ref_b],
        )

        assert result.status == "RESOLVED"
        assert [(b.file_id, b.binding_role) for b in result.bindings] == [
            (10, "requirement_source"),
            (20, "output_template"),
            (30, "reference_material"),
            (40, "reference_material"),
        ]


async def test_incompatible_explicit_binding_is_rejected(sqlite_session_factory):
    from app.services.task_attachment_resolver import TaskAttachmentResolver

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        req = await _seed_file(session, file_id=10, public_id="file_req", name="req.docx")
        png = await _seed_file(session, file_id=20, public_id="file_png", name="template.png", file_ext="png")

        result = await TaskAttachmentResolver(session).resolve(
            task_type="test_plan_generation",
            user_message="用《req.docx》作为需求，用《template.png》作为模板",
            ordered_files=[req, png],
            conversation_files=[req, png],
        )

        assert result.status == "UNSUPPORTED_ATTACHMENT"
        assert result.error_code == "attachment.incompatible_binding"
        assert result.bindings == []


async def test_persisted_binding_projects_legacy_ids(sqlite_session_factory):
    from app.repositories.attachment_understanding_repository import TaskFileBindingRepository
    from app.services.task_attachment_resolver import ResolvedTaskAttachment

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        await _seed_file(session, file_id=10, public_id="file_req", name="req.docx")
        await _seed_file(session, file_id=20, public_id="file_tpl", name="tpl.docx")
        task = AgentTask(
            id=100,
            public_id="task_1",
            user_id=1,
            conversation_id=1,
            task_type="test_plan_generation",
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(task)
        await session.flush()

        await TaskFileBindingRepository(session).persist_resolved_bindings(
            task=task,
            bindings=[
                ResolvedTaskAttachment(10, "requirement_source", 0, True, "automatic", 0.95),
                ResolvedTaskAttachment(20, "output_template", 0, True, "automatic", 0.95),
            ],
        )
        rows = (await session.execute(select(TaskFileBinding).order_by(TaskFileBinding.binding_role.asc()))).scalars().all()

        assert task.requirement_file_id == 10
        assert task.template_file_id == 20
        assert {(row.file_id, row.binding_role, row.is_primary) for row in rows} == {
            (10, "requirement_source", True),
            (20, "output_template", True),
        }


async def test_legacy_task_projection_still_works(sqlite_session_factory):
    from app.repositories.attachment_understanding_repository import TaskFileBindingRepository

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        await _seed_file(session, file_id=10, public_id="file_req", name="req.docx")
        await _seed_file(session, file_id=20, public_id="file_tpl", name="tpl.docx")
        task = AgentTask(
            id=100,
            public_id="task_legacy",
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

        await TaskFileBindingRepository(session).create_legacy_projection(task)
        rows = await TaskFileBindingRepository(session).list_for_task(task.id, user_id=1)

        assert [(row.file_id, row.binding_role, row.binding_source) for row in rows] == [
            (10, "requirement_source", "legacy"),
            (20, "output_template", "legacy"),
        ]


async def test_retry_resume_uses_persisted_bindings_not_new_conversation_guess(sqlite_session_factory):
    from app.repositories.attachment_understanding_repository import TaskFileBindingRepository
    from app.services.task_attachment_resolver import ResolvedTaskAttachment

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        await _seed_file(session, file_id=10, public_id="file_req_old", name="old-req.docx")
        await _seed_file(session, file_id=20, public_id="file_tpl_old", name="old-tpl.docx")
        await _seed_file(session, file_id=30, public_id="file_req_new", name="new-req.docx")
        await _seed_file(session, file_id=40, public_id="file_tpl_new", name="new-tpl.docx")
        task = AgentTask(
            id=100,
            public_id="task_stable",
            user_id=1,
            conversation_id=1,
            task_type="test_plan_generation",
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(task)
        await session.flush()
        repo = TaskFileBindingRepository(session)
        await repo.persist_resolved_bindings(
            task=task,
            bindings=[
                ResolvedTaskAttachment(10, "requirement_source", 0, True, "automatic", 0.95),
                ResolvedTaskAttachment(20, "output_template", 0, True, "automatic", 0.95),
            ],
        )

        rows = await repo.list_for_task(task.id, user_id=1)

        assert [row.file_id for row in rows] == [10, 20]
        assert task.requirement_file_id == 10
        assert task.template_file_id == 20


async def test_normal_chat_does_not_require_binding(sqlite_session_factory):
    from app.services.task_attachment_resolver import TaskAttachmentResolver

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        note = await _seed_file(session, file_id=10, public_id="file_note", name="note.md", file_ext="md")

        result = await TaskAttachmentResolver(session).resolve(
            task_type="normal_chat",
            user_message="这份文件讲了什么",
            ordered_files=[note],
            conversation_files=[note],
        )

        assert result.status == "RESOLVED"
        assert result.bindings == []


async def test_create_agent_task_uses_resolver_profiles_and_persists_bindings(
    sqlite_session_factory,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.agent.enums import IntentType, MessageRoute
    from app.agent.intent_router import IntentResult
    from app.services.message_service import MessageService

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        req = await _seed_file(session, file_id=10, public_id="file_req", name="notes.md", file_ext="md")
        tpl = await _seed_file(session, file_id=20, public_id="file_tpl", name="layout.docx")
        await _seed_profile(session, uploaded=req, document_kind="requirements_specification", possible_usages=["requirement_source"])
        await _seed_profile(session, uploaded=tpl, document_kind="test_plan_template", possible_usages=["output_template"])

        ms = MessageService.__new__(MessageService)
        ms._session = session
        ms._task_repo = MagicMock()
        ms._event_repo = MagicMock()
        ms._event_repo.create = AsyncMock()
        ms._exec_repo = MagicMock()
        ms._exec_repo.enqueue_new_task = AsyncMock(return_value=SimpleNamespace())
        ms._task_file_binding_repo = MagicMock()
        ms._task_file_binding_repo.persist_resolved_bindings = AsyncMock()
        ms._context_svc = MagicMock()
        ms._context_svc.build_task_trigger_context = AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda mode=None: {})
        )

        captured = {}

        async def _capture(task):
            task.id = 100
            captured["task"] = task
            return task

        ms._task_repo.create = _capture
        intent = IntentResult(
            intent=IntentType.TEST_PLAN_GENERATION,
            route=MessageRoute.AGENT_TASK,
            supported=True,
            confidence=1.0,
            reason="",
        )

        await ms._create_agent_task(
            conv=SimpleNamespace(id=1, public_id="conv_1", title="Test"),
            user_internal_id=1,
            content="生成测试方案",
            now=_now(),
            user_msg_dict={"message_id": "msg_1"},
            intent_result=intent,
            files=[req, tpl],
            attached_id_set={"file_req", "file_tpl"},
            user_msg_internal_id=500,
        )

        task = captured["task"]
        assert task.requirement_file_id == 10
        assert task.template_file_id == 20
        persisted = ms._task_file_binding_repo.persist_resolved_bindings.await_args.kwargs["bindings"]
        assert [(b.file_id, b.binding_role) for b in persisted] == [
            (10, "requirement_source"),
            (20, "output_template"),
        ]
