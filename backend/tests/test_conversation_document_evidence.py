"""Focused fail-closed contracts for conversation-scoped document evidence."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind
from app.context_engine.sources.conversation_document_evidence import ConversationDocumentEvidenceSourceAdapter


def _request(call_site: str) -> ContextRequest:
    return ContextRequest(
        user_id="7", conversation_id="11", conversation_public_id="conv_11",
        call_site=call_site, current_user_message="What booking rules does the requirement contain?",
    )


def _scope() -> ContextScope:
    return ContextScope(user_id="7", conversation_id="11", workspace_key="conversation:conv_11")


def _section() -> SectionPlan:
    return SectionPlan(kind=ContextKind.EVIDENCE, budget_tokens=1000, allow_retrieval=True)


def test_document_evidence_does_not_run_for_ordinary_chat() -> None:
    result = asyncio.run(ConversationDocumentEvidenceSourceAdapter().collect(
        _request("chat.reply"), _section(), _scope(), runtime_context=SimpleNamespace()
    ))
    assert result.attempted is False
    assert result.items == []


def test_document_evidence_fails_closed_when_runtime_is_unavailable() -> None:
    result = asyncio.run(ConversationDocumentEvidenceSourceAdapter().collect(
        _request("document.qa"), _section(), _scope(), runtime_context=SimpleNamespace(),
    ))
    assert result.attempted is True
    assert result.degraded is True
    assert result.items == []
    assert result.failure_code == "context.source.document_runtime_unavailable"


@pytest.mark.asyncio
async def test_document_evidence_resolves_requirement_and_loads_all_ordered_chunks(sqlite_session_factory) -> None:
    """Document-QA loads the complete resolved requirement, never a top-K subset."""
    from app.models.context_engine import ContextIndexChunk, ContextIndexDocument
    from app.repositories.base import ensure_model_id

    async with sqlite_session_factory() as session:
        document = ContextIndexDocument(
            public_id="idoc_requirement", user_id=7, workspace_key="conversation:conv_11",
            source_type="uploaded_file", source_public_id="file_requirement", source_version="2",
            source_digest="digest_requirement", title="Requirement.docx", status="indexed",
            chunk_policy_key="recursive_char:v1", chunk_policy_version="v1",
            lexical_index_status="ready", vector_index_status="skipped", idempotency_key="idem_requirement",
            metadata_json={"document_kind": "requirements_specification"},
        )
        await ensure_model_id(session, ContextIndexDocument, document)
        session.add(document)
        await session.flush()
        first = ContextIndexChunk(
            public_id="ick_requirement_intro", document_id=document.id, user_id=7, chunk_index=0,
            content="The platform manages campus reservations.", content_hash="chunk_requirement_intro",
            char_count=39, section_path="1 Overview", status="active",
        )
        second = ContextIndexChunk(
            public_id="ick_booking_rules", document_id=document.id, user_id=7, chunk_index=1,
            content="Booking is allowed only after approval.", content_hash="chunk_requirement_rules",
            char_count=39, section_path="3.2 Booking rules", status="active",
        )
        await ensure_model_id(session, ContextIndexChunk, first)
        session.add(first)
        await session.flush()
        await ensure_model_id(session, ContextIndexChunk, second)
        session.add(second)
        await session.commit()

    class _UnexpectedRetrievalExecutor:
        async def execute(self, **_kwargs):
            raise AssertionError("document.qa must not call generic retrieval")

    result = await ConversationDocumentEvidenceSourceAdapter(
        executor=_UnexpectedRetrievalExecutor(), retrieval_enabled=True,
    ).collect(
        _request("document.qa"), _section(), _scope(),
        runtime_context=SimpleNamespace(
            session_factory=sqlite_session_factory, user_internal_id=7,
        ),
    )

    assert result.failure_code is None
    assert len(result.items) == 1
    evidence = result.items[0]
    assert evidence.item_id == "conversation_document_full:idoc_requirement"
    assert evidence.metadata["whole_document"] is True
    assert evidence.metadata["chunk_public_ids"] == ["ick_requirement_intro", "ick_booking_rules"]
    assert evidence.content.index("campus reservations") < evidence.content.index("only after approval")
    assert evidence.metadata["citation"] == {
        "source_id": "file_requirement", "document_id": "idoc_requirement",
        "version": "2", "title": "Requirement.docx", "section": None, "role": "uploaded_file",
    }


@pytest.mark.asyncio
async def test_document_evidence_blocks_instead_of_compacting_an_oversized_document(sqlite_session_factory) -> None:
    from app.models.context_engine import ContextIndexChunk, ContextIndexDocument
    from app.repositories.base import ensure_model_id

    async with sqlite_session_factory() as session:
        document = ContextIndexDocument(
            public_id="idoc_large", user_id=7, workspace_key="conversation:conv_11",
            source_type="uploaded_file", source_public_id="file_large", source_version="1",
            source_digest="digest_large", title="Large Requirement.docx", status="indexed",
            chunk_policy_key="recursive_char:v1", chunk_policy_version="v1",
            lexical_index_status="ready", vector_index_status="skipped", idempotency_key="idem_large",
            metadata_json={"document_kind": "requirements_specification"},
        )
        await ensure_model_id(session, ContextIndexDocument, document)
        session.add(document)
        await session.flush()
        chunk = ContextIndexChunk(
            public_id="ick_large", document_id=document.id, user_id=7, chunk_index=0,
            content="a" * 25_000, content_hash="chunk_large", char_count=25_000, status="active",
        )
        await ensure_model_id(session, ContextIndexChunk, chunk)
        session.add(chunk)
        await session.commit()

    result = await ConversationDocumentEvidenceSourceAdapter().collect(
        _request("document.qa").model_copy(update={"model_context_window": 10_000}),
        _section(), _scope(),
        runtime_context=SimpleNamespace(session_factory=sqlite_session_factory, user_internal_id=7),
    )

    assert result.items == []
    assert result.failure_code == "context.source.document_full_content_too_large"
