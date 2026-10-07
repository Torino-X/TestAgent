"""Stage 3 HA contracts for index and retrieval truthfulness."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.context_engine.indexing.index_worker import IndexWorker
from app.context_engine.indexing.stores_protocol import RetrievedChunk
from app.context_engine.models.enums import RerankStrategy
from app.context_engine.models.retrieval import RetrievalRequest, RetrievalScopeFilter
from app.context_engine.retrieval.executor import RetrievalExecutor
from app.context_engine.retrieval.retrieval import RecheckResult, mysql_authoritative_recheck


class _Session:
    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lexical", "vector", "expected"),
    [
        ("ready", "failed", "indexed"),
        ("failed", "skipped", "failed"),
        ("skipped", "skipped", "failed"),
        ("pending", "skipped", "processing"),
    ],
)
async def test_document_terminal_status_requires_a_ready_channel(
    lexical: str,
    vector: str,
    expected: str,
) -> None:
    document = SimpleNamespace(
        lexical_index_status=lexical,
        vector_index_status=vector,
        status="processing",
        indexed_at=None,
    )
    worker = IndexWorker(session_factory=lambda: None)

    await worker._aggregate_document_status(_Session(), document)

    assert document.status == expected
    assert (document.indexed_at is not None) is (expected == "indexed")


def test_lexical_namespace_does_not_depend_on_embedding_configuration() -> None:
    from app.context_engine.indexing.worker_factory import build_index_namespaces

    without_embedding = build_index_namespaces(
        embedding_model=None,
        embedding_dimension=None,
        embedding_normalize=True,
        chunk_policy_key="recursive_char:v1",
    )
    with_embedding = build_index_namespaces(
        embedding_model="embed-v1",
        embedding_dimension=1024,
        embedding_normalize=True,
        chunk_policy_key="recursive_char:v1",
    )

    assert without_embedding.lexical is not None
    assert without_embedding.lexical == with_embedding.lexical
    assert without_embedding.vector is None
    assert with_embedding.vector is not None


class _EmptyLexicalStore:
    enabled = True

    async def search(self, **kwargs):
        return []


class _OneLexicalStore:
    enabled = True

    async def search(self, **kwargs):
        return [
            RetrievedChunk(
                chunk_public_id="chunk-1",
                document_public_id="doc-1",
                user_id=1,
                workspace_key=None,
                score=0.9,
                channel="lexical",
                rank=1,
            )
        ]


class _EmptyVectorStore:
    enabled = True

    def __init__(self):
        self.calls = 0

    async def search(self, **kwargs):
        self.calls += 1
        return []


class _EmbeddingProvider:
    def __init__(self):
        self.calls = 0

    async def embed(self, request):
        self.calls += 1
        return SimpleNamespace(
            vectors=[SimpleNamespace(values=[0.1, 0.2])]
        )


class _AuditExecutor(RetrievalExecutor):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.audit_calls: list[dict] = []

    async def _audit(self, **kwargs):
        self.audit_calls.append(kwargs)
        return "run-stage3"


def _request(*, rerank: RerankStrategy = RerankStrategy.WEIGHTED_RRF):
    return RetrievalRequest(
        query_text="release checklist",
        source_family="knowledge",
        top_k=5,
        rerank_strategy=rerank,
        scope=RetrievalScopeFilter(mode="user_global", user_id=1),
    )


@pytest.mark.asyncio
async def test_empty_retrieval_still_writes_a_completed_audit_run() -> None:
    executor = _AuditExecutor(
        lexical_store=_EmptyLexicalStore(),
        lexical_namespace="ctx_lex_test",
    )

    hits, run_id = await executor.execute(
        request=_request(),
        session=object(),
        user_internal_id=1,
        workspace_key=None,
    )

    assert hits == []
    assert run_id == "run-stage3"
    assert executor.audit_calls[0]["recalled_count"] == 0
    assert executor.audit_calls[0]["selected_count"] == 0
    assert executor.audit_calls[0]["rerank_on"] is False


@pytest.mark.asyncio
async def test_channel_policy_can_disable_a_physically_available_channel() -> None:
    executor = _AuditExecutor(
        lexical_store=_EmptyLexicalStore(),
        lexical_namespace="ctx_lex_test",
    )

    hits, run_id = await executor.execute(
        request=_request(),
        session=object(),
        user_internal_id=1,
        workspace_key=None,
        lexical_allowed=False,
        vector_allowed=False,
    )

    assert hits == []
    assert run_id == "run-stage3"
    assert executor.audit_calls[0]["lexical_on"] is False
    assert executor.audit_calls[0]["fallback_code"] == "context.retrieval.no_channel_enabled"


@pytest.mark.asyncio
async def test_dense_channel_executes_without_lexical_or_hybrid() -> None:
    vector_store = _EmptyVectorStore()
    embedding_provider = _EmbeddingProvider()
    executor = _AuditExecutor(
        vector_store=vector_store,
        embedding_provider=embedding_provider,
        embedding_dimension=2,
        embedding_model="embed-test",
        embedding_namespace="ctx_vec_test",
    )

    hits, run_id = await executor.execute(
        request=_request(),
        session=object(),
        user_internal_id=1,
        workspace_key=None,
        lexical_allowed=False,
        vector_allowed=True,
        hybrid_allowed=False,
    )

    assert hits == []
    assert run_id == "run-stage3"
    assert embedding_provider.calls == 1
    assert vector_store.calls == 1
    assert executor.audit_calls[0]["lexical_on"] is False
    assert executor.audit_calls[0]["vector_on"] is True


class _FailingReranker:
    enabled = True

    async def rerank(self, request):
        raise RuntimeError("provider down")


@pytest.mark.asyncio
async def test_reranker_failure_degrades_to_rrf_and_audits_actual_execution(monkeypatch) -> None:
    async def _approve(**kwargs):
        approved = {
            "chunk_public_id": "chunk-1",
            "document_public_id": "doc-1",
            "document_id": 1,
            "chunk_id": 1,
            "user_id": 1,
            "workspace_key": None,
            "content": "release checklist content",
            "content_hash": "hash-1",
            "estimated_tokens": 4,
            "source_public_id": "file-1",
            "source_version": "1",
        }
        return RecheckResult(
            approved=[
                approved
            ],
            observed_by_chunk={"chunk-1": approved},
        )

    monkeypatch.setattr(
        "app.context_engine.retrieval.executor.mysql_authoritative_recheck",
        _approve,
    )
    executor = _AuditExecutor(
        lexical_store=_OneLexicalStore(),
        lexical_namespace="ctx_lex_test",
        rerank_service=_FailingReranker(),
    )

    hits, run_id = await executor.execute(
        request=_request(rerank=RerankStrategy.RERANKER),
        session=object(),
        user_internal_id=1,
        workspace_key=None,
        rerank_allowed=True,
    )

    assert run_id == "run-stage3"
    assert len(hits) == 1
    assert hits[0].rerank_score is None
    assert executor.audit_calls[0]["rerank_on"] is False
    assert executor.audit_calls[0]["fallback_code"] == "context.retrieval.reranker_unavailable"


@pytest.mark.asyncio
async def test_authoritative_recheck_validates_the_candidate_channel_only(
    sqlite_session_factory,
) -> None:
    from app.models.context_engine import ContextIndexChunk, ContextIndexDocument
    from app.repositories.base import ensure_model_id

    async with sqlite_session_factory() as session:
        document = ContextIndexDocument(
            public_id="idoc_stage3_channel",
            user_id=1,
            workspace_key=None,
            source_type="uploaded_file",
            source_public_id="file-stage3",
            source_version="1",
            source_digest="digest-stage3",
            status="indexed",
            chunk_policy_key="recursive_char:v1",
            chunk_policy_version="v1",
            lexical_index_status="ready",
            vector_index_status="failed",
            idempotency_key="idem-stage3-channel",
        )
        await ensure_model_id(session, ContextIndexDocument, document)
        session.add(document)
        await session.flush()
        chunk = ContextIndexChunk(
            public_id="ick_stage3_channel",
            document_id=document.id,
            user_id=1,
            chunk_index=0,
            content="lexical evidence",
            content_hash="hash-stage3-channel",
            char_count=16,
            status="active",
        )
        await ensure_model_id(session, ContextIndexChunk, chunk)
        session.add(chunk)
        await session.commit()

    async with sqlite_session_factory() as session:
        result = await mysql_authoritative_recheck(
            session=session,
            chunk_public_ids=["ick_stage3_channel"],
            user_id=1,
            workspace_key=None,
            lexical_channel=True,
            vector_channel=True,
            channels_by_chunk={"ick_stage3_channel": {"lexical"}},
        )

    assert len(result.approved) == 1


def test_task_scoped_retrieval_policy_consumes_all_frozen_channel_flags() -> None:
    from app.context_engine.sources.knowledge import _retrieval_execution_policy

    values = {
        "CONTEXT_ENGINE_ENABLED": True,
        "CONTEXT_RETRIEVAL_ENABLED": True,
        "CONTEXT_LEXICAL_RETRIEVAL_ENABLED": True,
        "CONTEXT_DENSE_RETRIEVAL_ENABLED": True,
        "CONTEXT_HYBRID_FUSION_ENABLED": False,
        "CONTEXT_RERANK_ENABLED": True,
    }

    class _Resolver:
        def evaluate(self, name):
            return values[name]

    policy = _retrieval_execution_policy(
        SimpleNamespace(task_flag_resolver=_Resolver())
    )

    assert policy.enabled is True
    assert policy.lexical is True
    assert policy.dense is True
    assert policy.hybrid is False
    assert policy.rerank is True


def test_task_scoped_retrieval_policy_fails_closed_on_resolver_error() -> None:
    from app.context_engine.sources.knowledge import _retrieval_execution_policy

    class _BrokenResolver:
        def evaluate(self, name):
            raise RuntimeError("manifest unavailable")

    policy = _retrieval_execution_policy(
        SimpleNamespace(task_flag_resolver=_BrokenResolver())
    )

    assert policy.enabled is False
    assert policy.lexical is False
    assert policy.dense is False
    assert policy.hybrid is False
    assert policy.rerank is False


@pytest.mark.asyncio
async def test_retrieval_audit_records_actual_channel_and_policy(
    sqlite_session_factory,
) -> None:
    from sqlalchemy import select

    from app.context_engine.retrieval.audit import RetrievalAuditService
    from app.models.context_engine import ContextRetrievalRun

    async with sqlite_session_factory() as session:
        run_id = await RetrievalAuditService(session).record_knowledge_run(
            user_id=1,
            workspace_key=None,
            request=_request(),
            lexical_on=True,
            vector_on=False,
            rerank_on=False,
            recalled_count=0,
            reranked_count=0,
            selected_count=0,
            fallback_code="context.retrieval.no_hits",
            approved={},
            total_ms=1,
        )
        await session.commit()

    async with sqlite_session_factory() as session:
        row = (
            await session.execute(
                select(ContextRetrievalRun).where(
                    ContextRetrievalRun.public_id == run_id
                )
            )
        ).scalar_one()

    assert row.retrieval_channel == "lexical"
    assert row.retrieval_policy_key == "weighted_rrf"
    assert row.rerank_enabled is False


@pytest.mark.asyncio
async def test_knowledge_adapter_commits_retrieval_audit_transaction() -> None:
    from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
    from app.context_engine.models.enums import ContextKind
    from app.context_engine.sources.knowledge import KnowledgeSourceAdapter

    class _SessionWithCommit:
        def __init__(self):
            self.committed = False

        async def commit(self):
            self.committed = True

    session = _SessionWithCommit()

    class _SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Executor:
        async def execute(self, **kwargs):
            return [], "run-stage3-commit"

    adapter = KnowledgeSourceAdapter(executor=_Executor(), retrieval_enabled=True)
    result = await adapter.collect(
        ContextRequest(
            user_id="1",
            call_site="chat.reply",
            current_user_message="query",
        ),
        SectionPlan(
            kind=ContextKind.KNOWLEDGE,
            required=False,
            budget_tokens=100,
        ),
        ContextScope(user_id="1", thread_id="thread-1"),
        runtime_context=SimpleNamespace(
            session_factory=lambda: _SessionContext(),
            user_internal_id=1,
        ),
    )

    assert session.committed is True
    assert result.retrieval_run_ids == ["run-stage3-commit"]
