from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.exceptions import ForbiddenError
from app.models.attachment_understanding import FileSemanticProfile
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.uploaded_file import UploadedFile
from app.models.user import User
from app.services.file_service import FileService
from app.services.message_service import MessageService


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
    tmp_path,
    file_id: int,
    public_id: str,
    user_id: int,
    conv_id: int,
    original_name: str,
    content: str,
    created_at: datetime | None = None,
):
    storage_path = tmp_path / f"{public_id}.md"
    storage_path.write_text(content, encoding="utf-8")
    uploaded = UploadedFile(
        id=file_id,
        public_id=public_id,
        user_id=user_id,
        conversation_id=conv_id,
        original_name=original_name,
        stored_name=storage_path.name,
        file_ext=storage_path.suffix.lstrip("."),
        mime_type="text/markdown",
        file_size=len(content.encode("utf-8")),
        file_hash=f"hash-{public_id}",
        file_type="unknown",
        upload_status="uploaded",
        storage_type="local",
        storage_path=str(storage_path),
        created_at=created_at or _now(),
        updated_at=created_at or _now(),
    )
    session.add(uploaded)
    await session.flush()
    return uploaded


class ContentClassifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def classify(self, sample):
        text = str(sample)
        self.calls.append(text)
        if "Acceptance Criteria" in text or "Functional Requirements" in text:
            return {
                "document_kind": "requirements_specification",
                "summary": "Requirement document for checkout workflow.",
                "semantic_labels": ["requirements", "acceptance-criteria"],
                "possible_usages": ["requirement_source"],
                "confidence": 0.91,
            }
        if "Template Sections" in text or "Test Plan Template" in text:
            return {
                "document_kind": "test_plan_template",
                "summary": "Reusable test plan template.",
                "semantic_labels": ["template", "test-plan"],
                "possible_usages": ["output_template"],
                "confidence": 0.88,
            }
        return {
            "document_kind": "unknown",
            "summary": "Unknown document.",
            "semantic_labels": [],
            "possible_usages": [],
            "confidence": 0.1,
        }


def test_semantic_sampler_resolves_relative_storage_path_from_local_storage_base(monkeypatch, tmp_path):
    from app.services.file_understanding_service import FileSemanticSampler
    from app.storage.local_storage import local_storage

    rel_path = "uploads/1/conv_1/requirements.md"
    full_path = tmp_path / rel_path
    full_path.parent.mkdir(parents=True)
    full_path.write_text("# Checkout\nFunctional Requirements\nAcceptance Criteria", encoding="utf-8")
    monkeypatch.setattr(local_storage, "_base", tmp_path)

    uploaded = UploadedFile(
        public_id="file_relative",
        user_id=1,
        conversation_id=1,
        original_name="requirements.md",
        stored_name="requirements.md",
        file_ext="md",
        mime_type="text/markdown",
        file_size=full_path.stat().st_size,
        file_hash="hash-relative",
        file_type="unknown",
        upload_status="uploaded",
        storage_type="local",
        storage_path=rel_path,
        created_at=_now(),
        updated_at=_now(),
    )

    sample = FileSemanticSampler().build(uploaded)

    assert sample["structure"]["statistics"]["char_count"] > 0
    assert "Acceptance Criteria" in "\n".join(sample["representative_excerpts"])


async def test_non_standard_filename_requirement_profile_from_content(sqlite_session_factory, tmp_path):
    from app.services.file_understanding_service import FileUnderstandingService

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        await _seed_file(
            session,
            tmp_path=tmp_path,
            file_id=10,
            public_id="file_req",
            user_id=1,
            conv_id=1,
            original_name="random-notes.md",
            content="# Checkout\nFunctional Requirements\nAcceptance Criteria\n- Payment succeeds.",
        )
        classifier = ContentClassifier()

        profile = await FileUnderstandingService(session, classifier=classifier).understand_file(
            user_internal_id=1,
            file_public_id="file_req",
        )

        assert profile.status == "ready"
        assert profile.document_kind == "requirements_specification"
        assert profile.possible_usages_json == ["requirement_source"]
        assert profile.characteristics_json["evidence_contract_version"] == "file_semantic_evidence:v1"
        assert profile.characteristics_json["external_model_route"] == "file_semantic_classifier"
        assert "random-notes.md" in classifier.calls[0]
        assert "Acceptance Criteria" in classifier.calls[0]


async def test_non_standard_filename_template_profile_from_content(sqlite_session_factory, tmp_path):
    from app.services.file_understanding_service import FileUnderstandingService

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        await _seed_file(
            session,
            tmp_path=tmp_path,
            file_id=20,
            public_id="file_tpl",
            user_id=1,
            conv_id=1,
            original_name="document-42.md",
            content="# Test Plan Template\nTemplate Sections\nScope | Strategy | Risks | Cases",
        )

        profile = await FileUnderstandingService(
            session,
            classifier=ContentClassifier(),
        ).understand_file(user_internal_id=1, file_public_id="file_tpl")

        assert profile.status == "ready"
        assert profile.document_kind == "test_plan_template"
        assert profile.semantic_labels_json == ["template", "test-plan"]


async def test_misleading_filename_content_wins(sqlite_session_factory, tmp_path):
    from app.services.file_understanding_service import FileUnderstandingService

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        await _seed_file(
            session,
            tmp_path=tmp_path,
            file_id=30,
            public_id="file_misleading",
            user_id=1,
            conv_id=1,
            original_name="test_plan_template.md",
            content="# Checkout PRD\nFunctional Requirements\nAcceptance Criteria\n- Refund is audited.",
        )

        profile = await FileUnderstandingService(
            session,
            classifier=ContentClassifier(),
        ).understand_file(user_internal_id=1, file_public_id="file_misleading")

        assert profile.document_kind == "requirements_specification"
        assert profile.possible_usages_json == ["requirement_source"]


async def test_classifier_failure_marks_failed_without_raising(sqlite_session_factory, tmp_path):
    from app.services.file_understanding_service import FileUnderstandingService

    class BrokenClassifier:
        async def classify(self, sample):
            raise RuntimeError("model unavailable")

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        await _seed_file(
            session,
            tmp_path=tmp_path,
            file_id=40,
            public_id="file_bad",
            user_id=1,
            conv_id=1,
            original_name="opaque.md",
            content="# Anything\nFunctional Requirements",
        )

        profile = await FileUnderstandingService(
            session,
            classifier=BrokenClassifier(),
        ).understand_file(user_internal_id=1, file_public_id="file_bad")

        assert profile.status == "failed"
        assert profile.document_kind == "unknown"
        assert profile.error_code == "classification_failed"


async def test_file_understanding_profile_is_idempotent(sqlite_session_factory, tmp_path):
    from app.services.file_understanding_service import FileUnderstandingService

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        await _seed_file(
            session,
            tmp_path=tmp_path,
            file_id=50,
            public_id="file_once",
            user_id=1,
            conv_id=1,
            original_name="notes.md",
            content="# Checkout\nFunctional Requirements\nAcceptance Criteria",
        )
        classifier = ContentClassifier()
        service = FileUnderstandingService(session, classifier=classifier)

        first = await service.understand_file(user_internal_id=1, file_public_id="file_once")
        second = await service.understand_file(user_internal_id=1, file_public_id="file_once")
        profiles = (await session.execute(select(FileSemanticProfile))).scalars().all()

        assert first.id == second.id
        assert len(profiles) == 1
        assert len(classifier.calls) == 1


async def test_message_attachment_positions_ignore_upload_completion_time(sqlite_session_factory, tmp_path):
    from app.repositories.attachment_understanding_repository import MessageAttachmentRepository

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        file_c = await _seed_file(
            session,
            tmp_path=tmp_path,
            file_id=30,
            public_id="file_c",
            user_id=1,
            conv_id=1,
            original_name="c.md",
            content="c",
            created_at=_now(),
        )
        file_a = await _seed_file(
            session,
            tmp_path=tmp_path,
            file_id=10,
            public_id="file_a",
            user_id=1,
            conv_id=1,
            original_name="a.md",
            content="a",
            created_at=_now() + timedelta(seconds=20),
        )
        file_b = await _seed_file(
            session,
            tmp_path=tmp_path,
            file_id=20,
            public_id="file_b",
            user_id=1,
            conv_id=1,
            original_name="b.md",
            content="b",
            created_at=_now() + timedelta(seconds=10),
        )
        msg = Message(
            id=100,
            public_id="msg_1",
            user_id=1,
            conversation_id=1,
            role="user",
            message_type="user_text",
            content="use files in my order",
            payload_json={"attached_file_ids": ["file_a", "file_b", "file_c"]},
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(msg)
        await session.flush()

        await MessageAttachmentRepository(session).create_ordered_for_message(
            message=msg,
            files=[file_a, file_b, file_c],
        )
        attachments = await MessageAttachmentRepository(session).list_for_message(msg.id, user_id=1)
        detail = MessageService._to_detail(msg, "conv_1", files=[file_c, file_a, file_b])

        assert [(row.file_id, row.position) for row in attachments] == [(10, 0), (20, 1), (30, 2)]
        assert [row["file_id"] for row in detail["attached_files"]] == ["file_a", "file_b", "file_c"]


async def test_message_attachment_repository_rejects_cross_user_file(sqlite_session_factory, tmp_path):
    from app.repositories.attachment_understanding_repository import MessageAttachmentRepository

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session, user_id=1, conv_id=1)
        await _seed_user_conversation(session, user_id=2, conv_id=2)
        other = await _seed_file(
            session,
            tmp_path=tmp_path,
            file_id=60,
            public_id="file_other",
            user_id=2,
            conv_id=2,
            original_name="other.md",
            content="other",
        )
        msg = Message(
            id=100,
            public_id="msg_1",
            user_id=1,
            conversation_id=1,
            role="user",
            message_type="user_text",
            content="attach",
            payload_json={"attached_file_ids": ["file_other"]},
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(msg)
        await session.flush()

        with pytest.raises(ForbiddenError):
            await MessageAttachmentRepository(session).create_ordered_for_message(
                message=msg,
                files=[other],
            )


async def test_upload_survives_file_understanding_enqueue_failure(
    sqlite_session_factory,
    monkeypatch,
):
    async def noop_index(self, user_internal_id, uploaded_file):
        return None

    async def boom_understanding(self, user_internal_id, uploaded_file):
        raise RuntimeError("classifier queue down")

    monkeypatch.setattr(FileService, "_maybe_enqueue_internal_index", noop_index)
    monkeypatch.setattr(FileService, "_maybe_enqueue_file_understanding", boom_understanding, raising=False)

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)

        result = await FileService(session).upload(
            b"# Test Plan Template\nTemplate Sections",
            "template.md",
            1,
            "conv_1",
            content_type="text/markdown",
        )

        assert result["original_name"] == "template.md"
        assert result["upload_status"] == "uploaded"
