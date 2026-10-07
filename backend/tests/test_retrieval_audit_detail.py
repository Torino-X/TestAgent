"""Candidate-level Retrieval Audit Detail regression tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1.audit_serializers import serialize_retrieval_candidate
from app.api.v1.context_audit import get_retrieval, list_retrieval
from app.models.context_engine import (
    ContextIndexChunk,
    ContextIndexDocument,
    ContextRetrievalCandidate,
    ContextRetrievalRun,
)
from app.repositories.base import ensure_model_id
from app.schemas.auth import UserProfile


def _current(user_id: int, *, role: str = "user") -> UserProfile:
    return UserProfile(id=f"user_{user_id}", internal_id=user_id, name="tester", role=role)


def test_candidate_serializer_redacts_and_never_exposes_internal_ids():
    row = SimpleNamespace(
        source_type="knowledge",
        source_public_id="file_1",
        content_excerpt="Bearer fake-token api_key=fake-secret-sentinel",
        estimated_tokens=12,
        raw_rank=4,
        raw_score=0.4,
        normalized_score=0.5,
        rerank_score=0.9,
        final_rank=1,
        selected=True,
        drop_reason=None,
        source_quota_key="file:file_1",
        metadata_json={
            "channel": "lexical",
            "api_key": "fake-secret-sentinel",
            "unapproved": "must-not-leak",
        },
        index_document_id=101,
        index_chunk_id=202,
    )

    result = serialize_retrieval_candidate(
        row,
        index_document_public_id="idoc_1",
        index_chunk_public_id="ick_1",
    )

    assert result["index_document_public_id"] == "idoc_1"
    assert result["index_chunk_public_id"] == "ick_1"
    assert result["metadata"] == {"channel": "lexical"}
    assert result["selected"] is True
    assert "fake-secret-sentinel" not in str(result)
    assert "Bearer fake-token" not in str(result)
    assert "index_document_id" not in result
    assert "index_chunk_id" not in result


async def _seed_detail_rows(session_factory) -> None:
    async with session_factory() as session:
        run = ContextRetrievalRun(
            public_id="crr_owner",
            user_id=1,
            call_site="context.retrieval.knowledge",
            retrieval_channel="lexical",
            retrieval_policy_key="weighted_rrf",
            retrieval_policy_version="v1",
            query_hash="q" * 64,
            query_excerpt="safe query",
            requested_candidate_limit=10,
            requested_final_limit=2,
        recalled_count=2,
        selected_count=1,
        total_latency_ms=123,
        )
        await ensure_model_id(session, ContextRetrievalRun, run)
        session.add(run)
        await session.flush()

        document = ContextIndexDocument(
            public_id="idoc_owner",
            user_id=1,
            source_type="uploaded_file",
            source_public_id="file_owner",
            source_digest="d" * 64,
            status="indexed",
            chunk_policy_key="recursive_char:v1",
            chunk_policy_version="v1",
            lexical_index_status="ready",
            vector_index_status="skipped",
            idempotency_key="doc-owner",
        )
        await ensure_model_id(session, ContextIndexDocument, document)
        session.add(document)
        await session.flush()

        selected_chunk = ContextIndexChunk(
            public_id="ick_selected",
            document_id=document.id,
            user_id=1,
            chunk_index=0,
            content="selected evidence",
            content_hash="a" * 64,
            char_count=17,
            status="active",
        )
        dropped_chunk = ContextIndexChunk(
            public_id="ick_dropped",
            document_id=document.id,
            user_id=1,
            chunk_index=1,
            content="dropped structural heading",
            content_hash="b" * 64,
            char_count=26,
            status="active",
        )
        for chunk in (selected_chunk, dropped_chunk):
            await ensure_model_id(session, ContextIndexChunk, chunk)
            session.add(chunk)
        await session.flush()

        for candidate in (
            ContextRetrievalCandidate(
                retrieval_run_id=run.id,
                user_id=1,
                source_type="knowledge",
                source_public_id="file_owner",
                index_document_id=document.id,
                index_chunk_id=selected_chunk.id,
                content_hash="a" * 64,
                content_excerpt="selected evidence",
                raw_rank=2,
                final_rank=1,
                selected=True,
                metadata_json={"channel": "lexical"},
            ),
            ContextRetrievalCandidate(
                retrieval_run_id=run.id,
                user_id=1,
                source_type="knowledge",
                source_public_id="file_owner",
                index_document_id=document.id,
                index_chunk_id=dropped_chunk.id,
                content_hash="b" * 64,
                content_excerpt="dropped structural heading",
                raw_rank=3,
                selected=False,
                drop_reason="non_evidentiary",
                metadata_json={"channel": "lexical"},
            ),
            # Deliberately malformed cross-user row: repository owner scope must hide it.
            ContextRetrievalCandidate(
                retrieval_run_id=run.id,
                user_id=2,
                source_type="knowledge",
                source_public_id="file_other",
                content_hash="c" * 64,
                content_excerpt="other user evidence",
                raw_rank=1,
                selected=True,
            ),
        ):
            await ensure_model_id(session, ContextRetrievalCandidate, candidate)
            session.add(candidate)
        await session.commit()


async def test_retrieval_detail_returns_owner_scoped_safe_candidates(sqlite_session_factory):
    await _seed_detail_rows(sqlite_session_factory)

    async with sqlite_session_factory() as session:
        response = await get_retrieval(
            public_id="crr_owner",
            current=_current(1),
            session=session,
        )

    data = response["data"]
    assert data["public_id"] == "crr_owner"  # Existing summary contract remains.
    assert data["total_ms"] == 123
    assert data["candidate_count"] == 2
    assert [item["index_chunk_public_id"] for item in data["candidates"]] == [
        "ick_selected",
        "ick_dropped",
    ]
    assert data["candidates"][0]["selected"] is True
    assert data["candidates"][1]["drop_reason"] == "non_evidentiary"


async def test_retrieval_detail_hides_other_owners(sqlite_session_factory):
    await _seed_detail_rows(sqlite_session_factory)

    async with sqlite_session_factory() as session:
        with pytest.raises(HTTPException) as exc_info:
            await get_retrieval(
                public_id="crr_owner",
                current=_current(2),
                session=session,
            )

    assert exc_info.value.status_code == 404


async def test_retrieval_detail_keeps_existing_owner_scope_for_admin(sqlite_session_factory):
    """This user-facing route has no cross-owner admin bypass in its contract."""
    await _seed_detail_rows(sqlite_session_factory)

    async with sqlite_session_factory() as session:
        with pytest.raises(HTTPException) as exc_info:
            await get_retrieval(
                public_id="crr_owner",
                current=_current(2, role="admin"),
                session=session,
            )

    assert exc_info.value.status_code == 404


async def test_retrieval_list_remains_summary_only(sqlite_session_factory):
    await _seed_detail_rows(sqlite_session_factory)

    async with sqlite_session_factory() as session:
        response = await list_retrieval(current=_current(1), session=session, limit=20)

    item = response["data"]["items"][0]
    assert item["public_id"] == "crr_owner"
    assert "candidates" not in item
    assert "candidate_count" not in item


async def test_retrieval_audit_writer_persists_existing_decisions(sqlite_session_factory):
    """Writer records ranks/reasons only; it does not change selection."""
    from app.context_engine.models.enums import RerankStrategy, RetrievalStrategy
    from app.context_engine.models.retrieval import RetrievalRequest, RetrievalScopeFilter
    from app.context_engine.retrieval.audit import RetrievalAuditService

    request = RetrievalRequest(
        query_text="safe query",
        strategy=RetrievalStrategy.PLANNED,
        source_family="knowledge",
        top_k=1,
        requested_final_limit=1,
        rerank_strategy=RerankStrategy.WEIGHTED_RRF,
        scope=RetrievalScopeFilter(mode="union", user_id=1, workspace_key="ws_1"),
    )
    async with sqlite_session_factory() as session:
        run_public_id = await RetrievalAuditService(session).record_knowledge_run(
            user_id=1,
            workspace_key="ws_1",
            request=request,
            lexical_on=True,
            vector_on=False,
            rerank_on=False,
            recalled_count=1,
            reranked_count=0,
            selected_count=0,
            fallback_code=None,
            approved={
                "ick_dropped": {
                    "chunk_public_id": "ick_dropped",
                    "source_public_id": "file_1",
                    "content_hash": "d" * 64,
                    "content": "structural heading",
                    "raw_rank": 2,
                    "raw_score": 0.2,
                    "normalized_score": 0.1,
                    "selected": False,
                    "drop_reason": "non_evidentiary",
                    "channel": "lexical",
                    "channels": ["lexical"],
                    "channel_ranks": {"lexical": 2},
                    "channel_scores": {"lexical": 0.2},
                }
            },
            total_ms=7,
        )
        await session.commit()

        run = (
            await session.execute(
                select(ContextRetrievalRun).where(
                    ContextRetrievalRun.public_id == run_public_id
                )
            )
        ).scalar_one()
        candidate = (
            await session.execute(
                select(ContextRetrievalCandidate).where(
                    ContextRetrievalCandidate.retrieval_run_id == run.id
                )
            )
        ).scalar_one()

    assert candidate.raw_rank == 2
    assert candidate.drop_reason == "non_evidentiary"
    assert candidate.metadata_json == {
        "channel": "lexical",
        "channels": ["lexical"],
        "channel_ranks": {"lexical": 2},
        "channel_scores": {"lexical": 0.2},
    }
