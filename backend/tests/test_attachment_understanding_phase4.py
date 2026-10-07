from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from app.context_engine.indexing.stores_protocol import RetrievedChunk
from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind, RerankStrategy, RetrievalStrategy
from app.context_engine.models.retrieval import RetrievalRequest, RetrievalScopeFilter
from app.context_engine.retrieval.retrieval import mysql_authoritative_recheck
from app.models.attachment_understanding import FileSemanticProfile
from app.models.context_engine import ContextIndexChunk, ContextIndexDocument
from app.models.conversation import Conversation
from app.models.uploaded_file import UploadedFile
from app.models.user import User
from app.services.file_service import FileService


def _now() -> datetime:
    return datetime(2026, 8, 9, 12, 0, 0)


def _test_file_path(name: str) -> Path:
    root = Path("artifacts") / "attachment-understanding" / "phase4-test-files"
    root.mkdir(parents=True, exist_ok=True)
    return (root / name).resolve()


def test_index_worker_reads_relative_storage_path_from_local_storage_base(tmp_path, monkeypatch):
    from app.context_engine.indexing.index_worker import _read_storage
    from app.storage.local_storage import local_storage

    original_base = local_storage._base  # noqa: SLF001
    storage_base = tmp_path / "data"
    uploaded = storage_base / "uploads" / "1" / "conv_a" / "source.md"
    uploaded.parent.mkdir(parents=True)
    uploaded.write_bytes(b"# Source")
    monkeypatch.setattr(local_storage, "_base", storage_base)
    try:
        assert _read_storage("uploads\\1\\conv_a\\source.md") == b"# Source"
    finally:
        monkeypatch.setattr(local_storage, "_base", original_base)


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


async def test_index_worker_completion_clears_stale_error_state():
    from app.context_engine.indexing.index_worker import IndexWorker

    class FlushOnlySession:
        def __init__(self) -> None:
            self.flushed = False

        async def flush(self):
            self.flushed = True

    job = SimpleNamespace(
        status="pending",
        attempt=2,
        error_code="context.index.job_error",
        error_message="FileNotFoundError",
        completed_at=None,
    )
    session = FlushOnlySession()

    await IndexWorker(session_factory=None)._complete_job(session, job)  # type: ignore[arg-type]

    assert session.flushed is True
    assert job.status == "completed"
    assert job.error_code is None
    assert job.error_message is None


async def _seed_file(
    session,
    *,
    file_id: int,
    public_id: str,
    user_id: int = 1,
    conv_id: int = 1,
    name: str = "source.md",
    file_ext: str = "md",
    storage_path: str = "uploads/source.md",
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
        mime_type="text/markdown" if file_ext == "md" else f"image/{file_ext}",
        file_size=128,
        file_hash=f"hash-{public_id}",
        file_type=file_type,
        upload_status="uploaded",
        storage_type="local",
        storage_path=storage_path,
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
    document_kind: str = "requirements_specification",
    labels: list[str] | None = None,
    confidence: float = 0.91,
):
    profile = FileSemanticProfile(
        id=1000 + uploaded.id,
        public_id=f"fsp_{uploaded.id}",
        file_id=uploaded.id,
        user_id=uploaded.user_id,
        conversation_id=uploaded.conversation_id,
        status="ready",
        document_kind=document_kind,
        summary="This long summary must never be copied into index metadata.",
        semantic_labels_json=labels or ["requirements", "checkout"],
        possible_usages_json=["requirement_source"],
        characteristics_json={"sample_digest": "abc"},
        confidence=Decimal(str(confidence)),
        classifier_version="file_understanding:v1",
        source_hash=f"hash-{uploaded.public_id}",
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(profile)
    await session.flush()
    return profile


def _seed_index_rows(session, *, source_public_id: str = "file_req"):
    doc = ContextIndexDocument(
        id=200,
        public_id="idx_doc_1",
        user_id=1,
        workspace_key="conversation:conv_1",
        source_type="uploaded_file",
        source_public_id=source_public_id,
        source_version="1",
        source_digest="a" * 64,
        title="source.md",
        status="indexed",
        chunk_policy_key="recursive_char:v1",
        chunk_policy_version="v1",
        lexical_index_status="ready",
        vector_index_status="ready",
        metadata_json={"existing": "keep"},
        idempotency_key=f"idx:{source_public_id}",
        created_at=_now(),
        updated_at=_now(),
    )
    chunk = ContextIndexChunk(
        id=300,
        public_id="idx_chunk_1",
        document_id=200,
        user_id=1,
        chunk_index=0,
        content="checkout acceptance criteria",
        normalized_content="checkout acceptance criteria",
        content_hash="b" * 64,
        char_count=28,
        estimated_tokens=4,
        status="active",
        created_at=_now(),
        updated_at=_now(),
    )
    session.add_all([doc, chunk])
    return doc, chunk


async def test_index_submission_applies_safe_semantic_metadata_and_keeps_conversation_scope(
    sqlite_session_factory,
):
    from app.context_engine.indexing.document_service import IndexDocumentService
    from app.repositories.attachment_understanding_repository import FileSemanticProfileRepository

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        storage_path = _test_file_path("source.md")
        storage_path.write_text("# Checkout\nAcceptance Criteria", encoding="utf-8")
        uploaded = await _seed_file(
            session,
            file_id=10,
            public_id="file_req",
            storage_path=str(storage_path),
        )
        await _seed_profile(session, uploaded=uploaded)

        metadata = await FileSemanticProfileRepository(session).safe_index_metadata_for_file(
            uploaded_file=uploaded,
        )
        result = await IndexDocumentService(session).submit_uploaded_file(
            user_id=1,
            file_public_id="file_req",
        )
        document = (
            await session.execute(
                select(ContextIndexDocument).where(
                    ContextIndexDocument.public_id == result["document_public_id"]
                )
            )
        ).scalar_one()

        assert metadata == {
            "file_public_id": "file_req",
            "document_kind": "requirements_specification",
            "semantic_labels": ["requirements", "checkout"],
            "profile_confidence": 0.91,
            "profile_version": "file_understanding:v1",
        }
        assert document.workspace_key == "conversation:conv_1"
        assert document.metadata_json == metadata
        assert "summary" not in document.metadata_json
        assert "representative_excerpts" not in document.metadata_json


async def test_ready_profile_syncs_existing_index_document_without_copying_summary(
    sqlite_session_factory,
):
    from app.context_engine.indexing.document_service import IndexDocumentService

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        uploaded = await _seed_file(session, file_id=10, public_id="file_req")
        doc, _chunk = _seed_index_rows(session, source_public_id="file_req")
        await session.flush()
        await _seed_profile(session, uploaded=uploaded, document_kind="test_plan_template", labels=["template"])

        count = await IndexDocumentService(session).sync_uploaded_file_profile_metadata(
            user_id=1,
            file_public_id="file_req",
        )
        await session.refresh(doc)

        assert count == 1
        assert doc.metadata_json["existing"] == "keep"
        assert doc.metadata_json["file_public_id"] == "file_req"
        assert doc.metadata_json["document_kind"] == "test_plan_template"
        assert doc.metadata_json["semantic_labels"] == ["template"]
        assert "summary" not in doc.metadata_json


async def test_deleted_file_is_not_returned_by_mysql_authoritative_recheck(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        await _seed_file(session, file_id=10, public_id="file_req")
        _doc, chunk = _seed_index_rows(session, source_public_id="file_req")
        await session.flush()

        await FileService(session).delete(
            "file_req",
            user_internal_id=1,
            conv_public_id="conv_1",
        )
        recheck = await mysql_authoritative_recheck(
            session=session,
            chunk_public_ids=[chunk.public_id],
            user_id=1,
            workspace_key="conversation:conv_1",
            lexical_channel=True,
            vector_channel=False,
        )

        assert recheck.approved == []
        assert recheck.dropped_reasons == ["deleted"]


async def test_retrieval_does_not_hard_filter_by_document_kind(sqlite_session_factory):
    from app.context_engine.retrieval.executor import RetrievalExecutor

    class LexicalStore:
        enabled = True

        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def search(self, **kwargs):
            self.calls.append(kwargs)
            return [
                RetrievedChunk(
                    chunk_public_id="idx_chunk_1",
                    document_public_id="idx_doc_1",
                    user_id=1,
                    workspace_key="conversation:conv_1",
                    score=1.0,
                    channel="lexical",
                    rank=1,
                    source_public_id="file_req",
                    source_version="1",
                )
            ]

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        _doc, _chunk = _seed_index_rows(session, source_public_id="file_req")
        await session.flush()
        lexical = LexicalStore()
        request = RetrievalRequest(
            query_text="checkout",
            strategy=RetrievalStrategy.PLANNED,
            source_family="knowledge",
            top_k=5,
            rerank_strategy=RerankStrategy.WEIGHTED_RRF,
            scope=RetrievalScopeFilter(
                mode="workspace",
                user_id=1,
                workspace_key="conversation:conv_1",
            ),
        )

        results, _run_id = await RetrievalExecutor(
            lexical_store=lexical,
            lexical_namespace="lex",
        ).execute(
            request=request,
            session=session,
            user_internal_id=1,
            workspace_key="conversation:conv_1",
        )

        assert [result.source_public_id for result in results] == ["file_req"]
        assert "document_kind" not in lexical.calls[0]


async def test_file_document_source_adapter_uses_semantic_profile_not_legacy_file_type(
    sqlite_session_factory,
):
    from app.context_engine.sources.file_document import FileDocumentSourceAdapter

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        uploaded = await _seed_file(
            session,
            file_id=10,
            public_id="file_req",
            name="ambiguous.md",
            file_type="unknown",
        )
        await _seed_profile(session, uploaded=uploaded, labels=["profile-label"])
        await session.commit()

    request = ContextRequest(
        user_id="1",
        conversation_id="1",
        call_site="test",
        current_user_message="use the file",
    )
    result = await FileDocumentSourceAdapter().collect(
        request,
        SectionPlan(kind=ContextKind.EVIDENCE, budget_tokens=100),
        ContextScope(user_id="1", conversation_id="1"),
        runtime_context=SimpleNamespace(
            session_factory=sqlite_session_factory,
            user_internal_id=1,
        ),
    )

    assert [item.source_ref for item in result.items] == ["file_req"]
    assert result.items[0].metadata["document_kind"] == "requirements_specification"
    assert result.items[0].metadata["semantic_labels"] == ["profile-label"]


@dataclass
class _VisionCallResult:
    ok: bool = True
    summary: str = "flow diagram"
    error: str = ""

    def to_prompt_text(self, image_index: int, source: str = "") -> str:
        return f"image {image_index} {source}: {self.summary}"


async def test_current_message_image_vision_uses_main_model_when_it_supports_vision(
    sqlite_session_factory,
):
    from app.services.current_message_image_vision_service import CurrentMessageImageVisionService

    class MainVisionLLM:
        def __init__(self) -> None:
            self.images: list[str] = []

        async def generate_with_system(self, _system: str, user_content: str, *, images: list[str]):
            self.images = images
            return f"main vision reply: {user_content}"

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        image_path = _test_file_path("screen.png")
        image_path.write_bytes(b"fake png")
        image = await _seed_file(
            session,
            file_id=20,
            public_id="file_png",
            name="screen.png",
            file_ext="png",
            storage_path=str(image_path),
        )
        llm = MainVisionLLM()

        result = await CurrentMessageImageVisionService(
            session,
            main_llm_client=llm,
            main_model_supports_vision=True,
        ).generate_reply(
            user_content="What is in this image?",
            user_id=1,
            files=[image],
            attached_id_set={"file_png"},
        )

        assert result.handled is True
        assert result.reply == "main vision reply: What is in this image?"
        assert llm.images == [str(image_path)]


async def test_current_message_image_vision_falls_back_to_vision_service(
    sqlite_session_factory,
):
    from app.services.current_message_image_vision_service import CurrentMessageImageVisionService

    class FallbackVision:
        async def analyze_image(self, image_path, source, ocr_text, image_index):
            return _VisionCallResult(summary=f"analyzed {image_path.name}")

    class Chat:
        async def generate_reply(self, content: str, **_kwargs):
            return f"chat reply with [{content}]"

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        image_path = _test_file_path("screen.webp")
        image_path.write_bytes(b"fake webp")
        image = await _seed_file(
            session,
            file_id=20,
            public_id="file_webp",
            name="screen.webp",
            file_ext="webp",
            storage_path=str(image_path),
        )

        result = await CurrentMessageImageVisionService(
            session,
            chat_service=Chat(),
            main_model_supports_vision=False,
            vision_service_factory=lambda: FallbackVision(),
        ).generate_reply(
            user_content="Describe it",
            user_id=1,
            files=[image],
            attached_id_set={"file_webp"},
        )

        assert result.handled is True
        assert "analyzed screen.webp" in result.reply
        assert result.reply.startswith("chat reply with")


async def test_current_message_image_vision_handles_unsupported_attachments_gracefully(
    sqlite_session_factory,
):
    from app.services.current_message_image_vision_service import CurrentMessageImageVisionService

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        gif = await _seed_file(
            session,
            file_id=20,
            public_id="file_gif",
            name="anim.gif",
            file_ext="gif",
        )

        result = await CurrentMessageImageVisionService(session).generate_reply(
            user_content="Describe it",
            user_id=1,
            files=[gif],
            attached_id_set={"file_gif"},
        )

        assert result.handled is False
        assert result.error_code == "image.unsupported_type"


async def test_stream_chat_with_current_image_uses_image_vision_reply(sqlite_session_factory):
    from app.agent.enums import IntentType, MessageRoute
    from app.agent.intent_router import IntentResult
    from app.services.message_service import MessageService

    class VisionReply:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def generate_reply(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(handled=True, reply="vision stream reply")

    class PlainChat:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_reply(self, *_args, **_kwargs):
            self.calls += 1
            return "plain chat reply"

    async with sqlite_session_factory() as session:
        await _seed_user_conversation(session)
        conv = (
            await session.execute(select(Conversation).where(Conversation.id == 1))
        ).scalar_one()
        image_path = _test_file_path("stream-screen.png")
        image_path.write_bytes(b"fake png")
        image = await _seed_file(
            session,
            file_id=20,
            public_id="file_stream_png",
            name="stream-screen.png",
            file_ext="png",
            storage_path=str(image_path),
        )
        vision = VisionReply()
        chat = PlainChat()
        service = MessageService(
            session,
            chat_service=chat,
            current_image_vision_service=vision,
        )
        intent = IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=0.9,
            reason="current image question",
        )

        events = [
            event
            async for event in service._stream_assistant_reply(
                conv,
                1,
                "这张图里的错误是什么意思？",
                _now(),
                {"public_id": "message_user"},
                intent,
                [image],
                attached_id_set={"file_stream_png"},
            )
        ]

        deltas = [
            event["data"]["delta"]
            for event in events
            if event["event"] == "agent_text_delta"
        ]
        done = next(event for event in events if event["event"] == "agent_text_done")

        assert deltas == ["vision stream reply"]
        assert done["data"]["agent_reply"]["content"] == "vision stream reply"
        assert len(vision.calls) == 1
        assert vision.calls[0]["attached_id_set"] == {"file_stream_png"}
        assert chat.calls == 0
