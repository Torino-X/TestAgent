"""Regression coverage for artifact RAG sources and Markdown system policy."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError


def test_markdown_system_instruction_catalog_exposes_word_boundaries():
    from app.context_engine.system_instruction_catalog import (
        render_system_instructions,
        system_instruction_manifest,
    )

    rendered = render_system_instructions("chat.reply")
    assert "测试方案模板" in rendered
    assert "低层 Word 操作" in rendered
    manifest = system_instruction_manifest("chat.reply")
    assert {item["id"] for item in manifest} >= {"core", "test-plan-artifact-policy"}


@pytest.mark.asyncio
async def test_completed_artifact_is_submitted_as_project_private_index_source(
    tmp_path, sqlite_session_factory
):
    from app.context_engine.indexing.document_service import IndexDocumentService
    from app.models.artifact import Artifact
    from app.models.conversation import Conversation
    from app.models.project import Project
    from app.models.user import User
    from app.repositories.context_engine_repositories import ContextIndexDocumentRepository

    now = datetime.now()
    artifact_file = tmp_path / "plan.txt"
    artifact_file.write_text("支付回调必须校验幂等键", encoding="utf-8")
    async with sqlite_session_factory() as session:
        session.add(User(
            id=1, public_id="user_assets", username="assets", password_hash="x",
            role="user", status="active", created_at=now, updated_at=now,
        ))
        project = Project(
            id=1, public_id="proj_assets", user_id=1, name="Assets",
            context_workspace_key="project:proj_assets", status="active",
            memory_mode="inherit", created_at=now, updated_at=now,
        )
        session.add(project)
        conversation = Conversation(
            id=1, public_id="conv_assets", user_id=1, project_id=1,
            title="Assets", created_at=now, updated_at=now,
        )
        session.add(conversation)
        session.add(Artifact(
            id=1, public_id="art_assets", user_id=1, conversation_id=1, task_id=1,
            project_id=1, artifact_type="test_plan_word", file_name="plan.txt", file_ext="txt",
            file_size=artifact_file.stat().st_size, input_hash=None, storage_type="local",
            storage_path=str(artifact_file), status="available", version_no=1,
            created_at=now, updated_at=now,
        ))
        await session.commit()

        result = await IndexDocumentService(session).submit_artifact(
            user_id=1, artifact_public_id="art_assets"
        )
        document = await ContextIndexDocumentRepository(session).get_by_public_id(
            result["document_public_id"], 1
        )
        assert result["created"] is True
        assert document is not None
        assert document.source_type == "artifact"
        assert document.source_public_id == "art_assets"
        assert document.workspace_key == "project:proj_assets"


@pytest.mark.asyncio
async def test_project_question_reuses_indexed_artifact_chunk_with_locator(
    sqlite_session_factory,
):
    """End-to-end cache path: indexed plan section -> project question -> locator."""
    from app.models.context_engine import ContextIndexChunk, ContextIndexDocument
    from app.models.conversation import Conversation
    from app.models.project import Project
    from app.models.user import User
    from app.services.project_context_resolver import ProjectContextResolver

    now = datetime.now()
    async with sqlite_session_factory() as session:
        session.add(User(
            id=2, public_id="user_query", username="query", password_hash="x",
            role="user", status="active", created_at=now, updated_at=now,
        ))
        session.add(Project(
            id=2, public_id="proj_query", user_id=2, name="Query",
            context_workspace_key="project:proj_query", status="active",
            memory_mode="inherit", created_at=now, updated_at=now,
        ))
        session.add(Conversation(
            id=2, public_id="conv_query", user_id=2, project_id=2,
            title="Query", created_at=now, updated_at=now,
        ))
        document = ContextIndexDocument(
            id=20, public_id="idoc_plan", user_id=2,
            workspace_key="project:proj_query", source_type="artifact",
            source_public_id="art_plan", source_version="1", source_digest="a" * 64,
            title="支付测试方案.docx", status="indexed", chunk_policy_key="recursive_char:v1",
            chunk_policy_version="v1", lexical_index_status="ready", vector_index_status="skipped",
            metadata_json={"source_role": "generated_artifact", "is_current": True},
            idempotency_key="idem_plan", created_at=now, updated_at=now,
        )
        session.add(document)
        await session.flush()
        session.add(ContextIndexChunk(
            id=21, public_id="ick_plan", document_id=20, user_id=2, chunk_index=0,
            section_path="7 支付回调验证", content="支付回调必须校验幂等键，并记录订单号。",
            normalized_content="支付回调必须校验幂等键 并记录订单号", content_hash="b" * 64,
            char_count=22, estimated_tokens=10, status="active", created_at=now, updated_at=now,
        ))
        await session.commit()

        package = await ProjectContextResolver(
            session,
            evidence_cache_session_factory=sqlite_session_factory,
        ).resolve(
            user_id=2, conversation_id=2, query="测试方案的支付回调校验是什么？"
        )
        assert package["source_hits"]
        hit = package["source_hits"][0]
        assert hit["source_id"] == "art_plan"
        assert hit["section"] == "7 支付回调验证"
        assert hit["evidence_cache_id"].startswith("ctxev")


@pytest.mark.asyncio
async def test_project_context_keeps_fresh_hits_when_evidence_cache_deadlocks(
    monkeypatch,
    sqlite_session_factory,
):
    """Observability-cache failures must not poison the caller's chat transaction."""
    from app.models.context_engine import ContextIndexChunk, ContextIndexDocument
    from app.models.conversation import Conversation
    from app.models.project import Project
    from app.models.user import User
    from app.services.conversation_evidence_cache_service import (
        ConversationEvidenceCacheService,
    )
    from app.services.project_context_resolver import ProjectContextResolver

    now = datetime.now()
    async with sqlite_session_factory() as session:
        session.add(User(
            id=3, public_id="user_deadlock", username="deadlock", password_hash="x",
            role="user", status="active", created_at=now, updated_at=now,
        ))
        session.add(Project(
            id=3, public_id="proj_deadlock", user_id=3, name="Deadlock",
            context_workspace_key="project:proj_deadlock", status="active",
            memory_mode="inherit", created_at=now, updated_at=now,
        ))
        session.add(Conversation(
            id=3, public_id="conv_deadlock", user_id=3, project_id=3,
            title="Deadlock", created_at=now, updated_at=now,
        ))
        session.add(ContextIndexDocument(
            id=30, public_id="idoc_deadlock", user_id=3,
            workspace_key="project:proj_deadlock", source_type="project_file",
            source_public_id="src_deadlock", source_version="1", source_digest="c" * 64,
            title="deadlock.docx", status="indexed", chunk_policy_key="recursive_char:v1",
            chunk_policy_version="v1", lexical_index_status="ready", vector_index_status="skipped",
            metadata_json={}, idempotency_key="idem_deadlock", created_at=now, updated_at=now,
        ))
        await session.flush()
        session.add(ContextIndexChunk(
            id=31, public_id="ick_deadlock", document_id=30, user_id=3, chunk_index=0,
            section_path="deadlock", content="死锁后仍应保留本次新检索到的项目依据",
            normalized_content="死锁后仍应保留本次新检索到的项目依据", content_hash="d" * 64,
            char_count=18, estimated_tokens=8, status="active", created_at=now, updated_at=now,
        ))
        await session.commit()

        async def raise_deadlock(*_args, **_kwargs):
            error = RuntimeError(1213, "Deadlock found when trying to get lock")
            raise OperationalError("INSERT conversation_evidence_audits", {}, error)

        monkeypatch.setattr(
            ConversationEvidenceCacheService,
            "merge_with_retrieval",
            raise_deadlock,
        )
        package = await ProjectContextResolver(
            session,
            evidence_cache_session_factory=sqlite_session_factory,
        ).resolve(
            user_id=3,
            conversation_id=3,
            query="死锁项目依据",
        )

        assert [hit["chunk_id"] for hit in package["source_hits"]] == ["ick_deadlock"]
        assert (await session.execute(select(Project.id).where(Project.id == 3))).scalar_one() == 3


def test_evidence_cache_selects_pinned_item_before_newer_unpinned_item():
    from types import SimpleNamespace
    from app.services.conversation_evidence_cache_service import ConversationEvidenceCacheService

    pinned = SimpleNamespace(
        pinned=True, relevance_score=0, selected_count=0, id=1,
        estimated_tokens=100, locator_json={"chunk_id": "old"},
    )
    fresh = SimpleNamespace(
        pinned=False, relevance_score=99, selected_count=0, id=2,
        estimated_tokens=100, locator_json={"chunk_id": "fresh"},
    )
    selected = ConversationEvidenceCacheService._select([fresh, pinned], {"fresh"})
    assert selected == [pinned, fresh]


def test_artifact_policy_rejects_unsupported_low_level_word_format_operation():
    from app.capabilities.artifact_policy import unsupported_format_operations

    assert unsupported_format_operations({"format_operations": ["table_border"]}) == ["table_border"]
    assert unsupported_format_operations({"format_operations": ["content_backfill"]}) == []
