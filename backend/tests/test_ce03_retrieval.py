"""CE-03 Retrieval 测试：Weighted RRF / QueryBuilder / ScopeFilter / 两级 ACL。

覆盖：
- Weighted RRF 确定性 + 数学；
- QueryBuilder 由 Plan/SectionPlan 生成 typed RetrievalRequest（不依赖 user_message）；
- RetrievalScopeFilter 四模式匹配语义；
- MySQL 权威复核：stale external（MySQL 已删/未 ready）→ 结果 0。
"""

from __future__ import annotations

import pytest

from app.context_engine.models.context import ContextPlan, ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind, RetrievalStrategy, RerankStrategy
from app.context_engine.models.retrieval import RetrievalRequest, RetrievalScopeFilter
from app.context_engine.retrieval.retrieval import (
    FusionResult,
    QueryBuilder,
    mysql_authoritative_recheck,
    weighted_rrf,
)


def _fusion(cid, channel, rank, score):
    return FusionResult(
        chunk_public_id=cid,
        document_public_id=None,
        user_id=1,
        workspace_key=None,
        final_rank=rank,
        raw_score=score,
        normalized_score=score,
        rerank_score=None,
        channel=channel,
    )


def test_weighted_rrf_deterministic_and_ranks():
    a = [
        _fusion("c1", "lexical", 1, 0.9),
        _fusion("c2", "lexical", 2, 0.8),
    ]
    b = [
        _fusion("c2", "vector", 1, 0.95),
        _fusion("c3", "vector", 2, 0.7),
    ]
    r1 = weighted_rrf([a, b])
    r2 = weighted_rrf([a, b])
    assert [(m.chunk_public_id, m.final_rank) for m in r1] == [(m.chunk_public_id, m.final_rank) for m in r2]
    assert r1[0].chunk_public_id == "c2"  # 双通道命中 → 更高
    assert len(r1) == 3


def test_query_builder_from_plan_only():
    from app.context_engine.models.context import RetrievalQuery

    plan = ContextPlan(
        profile_key="p",
        profile_version="v1",
        model_context_window=8000,
        input_budget=4000,
        output_reserve=1000,
        runtime_reserve=500,
        safety_margin=200,
        retrieval_queries=[
            RetrievalQuery(
                query_text="query A",
                strategy=RetrievalStrategy.PLANNED,
                source_family="knowledge",
                top_k=5,
                rerank_strategy=RerankStrategy.WEIGHTED_RRF,
            )
        ],
    )
    reqs = QueryBuilder().build(
        user_internal_id=1,
        workspace_key="ws1",
        agent_type=None,
        plan=plan,
        section_plans=[],
    )
    assert len(reqs) == 1
    assert reqs[0].source_family == "knowledge"
    assert reqs[0].scope.user_id == 1
    assert reqs[0].scope.workspace_key == "ws1"


def test_query_builder_on_demand_from_section_allow_retrieval():
    section = SectionPlan(kind=ContextKind.KNOWLEDGE, required=False, budget_tokens=1000, allow_retrieval=True)
    reqs = QueryBuilder().build(
        user_internal_id=1,
        workspace_key=None,
        agent_type="prep",
        plan=None,
        section_plans=[section],
    )
    assert len(reqs) == 1
    assert reqs[0].source_family == "knowledge"
    assert reqs[0].strategy == RetrievalStrategy.ON_DEMAND


def test_scope_filter_four_modes():
    # user-global
    assert RetrievalScopeFilter(mode="user_global", user_id=1, workspace_key=None).matches_row(
        owner_user_id=1, owner_workspace_key=None
    )
    assert not RetrievalScopeFilter(mode="user_global", user_id=1, workspace_key=None).matches_row(
        owner_user_id=1, owner_workspace_key="ws1"
    )
    # workspace 精确
    assert RetrievalScopeFilter(mode="workspace", user_id=1, workspace_key="ws1").matches_row(
        owner_user_id=1, owner_workspace_key="ws1"
    )
    assert not RetrievalScopeFilter(mode="workspace", user_id=1, workspace_key="ws1").matches_row(
        owner_user_id=1, owner_workspace_key="ws2"
    )
    # union：user-global 或 当前 workspace
    assert RetrievalScopeFilter(mode="union", user_id=1, workspace_key="ws1").matches_row(
        owner_user_id=1, owner_workspace_key=None
    )
    assert RetrievalScopeFilter(mode="union", user_id=1, workspace_key="ws1").matches_row(
        owner_user_id=1, owner_workspace_key="ws1"
    )
    assert not RetrievalScopeFilter(mode="union", user_id=1, workspace_key="ws1").matches_row(
        owner_user_id=1, owner_workspace_key="ws2"
    )
    # playbook：user + agent_type 精确 + workspace null
    assert RetrievalScopeFilter(mode="playbook", user_id=1, agent_type="prep").matches_row(
        owner_user_id=1, owner_workspace_key=None, row_scope_type="agent_playbook", row_agent_type="prep"
    )
    assert not RetrievalScopeFilter(mode="playbook", user_id=1, agent_type="prep").matches_row(
        owner_user_id=1, owner_workspace_key=None, row_scope_type="agent_playbook", row_agent_type="other"
    )
    assert not RetrievalScopeFilter(mode="playbook", user_id=1, agent_type="prep").matches_row(
        owner_user_id=2, owner_workspace_key=None, row_scope_type="agent_playbook", row_agent_type="prep"
    )


async def test_mysql_recheck_stale_external_zero(sqlite_session_factory):
    """MySQL 已删除/未 ready，外部仍 active → 结果 0（stale external protection）。"""
    from datetime import datetime
    from app.models.context_engine import ContextIndexChunk, ContextIndexDocument
    from app.repositories.base import ensure_model_id

    sf = sqlite_session_factory
    async with sf() as session:
        doc = ContextIndexDocument(
            public_id="idoc_1", user_id=1, workspace_key=None,
            source_type="uploaded_file", source_public_id="file_1",
            source_version="1", source_digest="digest1",
            status="indexed", chunk_policy_key="recursive_char:v1",
            chunk_policy_version="v1", lexical_index_status="ready",
            vector_index_status="skipped", idempotency_key="idem1",
        )
        await ensure_model_id(session, ContextIndexDocument, doc)
        session.add(doc)
        await session.flush()
        # chunk deleted（外部仍 active 的 stale point 场景）
        chunk = ContextIndexChunk(
            public_id="ick_1", document_id=doc.id, user_id=1, chunk_index=0,
            content="content", content_hash="hash1", char_count=7, status="active",
            deleted_at=datetime(2026, 1, 1),
        )
        await ensure_model_id(session, ContextIndexChunk, chunk)
        session.add(chunk)
        await session.commit()

    async with sf() as session:
        recheck = await mysql_authoritative_recheck(
            session=session,
            chunk_public_ids=["ick_1"],
            user_id=1,
            workspace_key=None,
            lexical_channel=True,
            vector_channel=False,
        )
        assert len(recheck.approved) == 0
        assert "deleted" in recheck.dropped_reasons
        assert recheck.rejected_by_chunk == {"ick_1": "deleted"}
        assert recheck.observed_by_chunk["ick_1"]["chunk_id"] == chunk.id


async def test_mysql_recheck_channel_not_ready_dropped(sqlite_session_factory):
    """召回 channel 未 ready → stale_version drop。"""
    from app.models.context_engine import ContextIndexChunk, ContextIndexDocument
    from app.repositories.base import ensure_model_id

    sf = sqlite_session_factory
    async with sf() as session:
        doc = ContextIndexDocument(
            public_id="idoc_2", user_id=1, workspace_key=None,
            source_type="uploaded_file", source_public_id="file_2",
            source_version="1", source_digest="digest2",
            status="indexed", chunk_policy_key="recursive_char:v1",
            chunk_policy_version="v1", lexical_index_status="pending",
            vector_index_status="skipped", idempotency_key="idem2",
        )
        await ensure_model_id(session, ContextIndexDocument, doc)
        session.add(doc)
        await session.flush()
        chunk = ContextIndexChunk(
            public_id="ick_2", document_id=doc.id, user_id=1, chunk_index=0,
            content="content", content_hash="hash2", char_count=7, status="active",
        )
        await ensure_model_id(session, ContextIndexChunk, chunk)
        session.add(chunk)
        await session.commit()

    async with sf() as session:
        recheck = await mysql_authoritative_recheck(
            session=session,
            chunk_public_ids=["ick_2"],
            user_id=1,
            workspace_key=None,
            lexical_channel=True,
            vector_channel=False,
        )
        assert len(recheck.approved) == 0
        assert "stale_version" in recheck.dropped_reasons
        assert recheck.rejected_by_chunk == {"ick_2": "stale_version"}
        assert recheck.observed_by_chunk["ick_2"]["document_id"] == doc.id


async def test_mysql_recheck_approved_when_ready(sqlite_session_factory):
    """document indexed + channel ready + chunk active → approved。"""
    from app.models.context_engine import ContextIndexChunk, ContextIndexDocument
    from app.repositories.base import ensure_model_id

    sf = sqlite_session_factory
    async with sf() as session:
        doc = ContextIndexDocument(
            public_id="idoc_3", user_id=1, workspace_key=None,
            source_type="uploaded_file", source_public_id="file_3",
            source_version="1", source_digest="digest3",
            status="indexed", chunk_policy_key="recursive_char:v1",
            chunk_policy_version="v1", lexical_index_status="ready",
            vector_index_status="skipped", idempotency_key="idem3",
        )
        await ensure_model_id(session, ContextIndexDocument, doc)
        session.add(doc)
        await session.flush()
        chunk = ContextIndexChunk(
            public_id="ick_3", document_id=doc.id, user_id=1, chunk_index=0,
            content="hello world", content_hash="hash3", char_count=11, status="active",
        )
        await ensure_model_id(session, ContextIndexChunk, chunk)
        session.add(chunk)
        await session.commit()

    async with sf() as session:
        recheck = await mysql_authoritative_recheck(
            session=session,
            chunk_public_ids=["ick_3"],
            user_id=1,
            workspace_key=None,
            lexical_channel=True,
            vector_channel=False,
        )
        assert len(recheck.approved) == 1
        assert recheck.approved[0]["content"] == "hello world"


async def test_mysql_recheck_enforces_document_source_and_version_filters(sqlite_session_factory):
    """Document-QA candidates outside the requested material/version never enter context."""
    from app.models.context_engine import ContextIndexChunk, ContextIndexDocument
    from app.repositories.base import ensure_model_id

    async with sqlite_session_factory() as session:
        doc = ContextIndexDocument(
            public_id="idoc_filtered", user_id=1, workspace_key="conversation:conv_1",
            source_type="artifact", source_public_id="artifact_old", source_version="1",
            source_digest="digest_filtered", status="indexed", chunk_policy_key="recursive_char:v1",
            chunk_policy_version="v1", lexical_index_status="ready", vector_index_status="skipped",
            idempotency_key="idem_filtered",
        )
        await ensure_model_id(session, ContextIndexDocument, doc)
        session.add(doc)
        await session.flush()
        chunk = ContextIndexChunk(
            public_id="ick_filtered", document_id=doc.id, user_id=1, chunk_index=0,
            content="old artifact", content_hash="hash_filtered", char_count=12, status="active",
        )
        await ensure_model_id(session, ContextIndexChunk, chunk)
        session.add(chunk)
        await session.commit()

    async with sqlite_session_factory() as session:
        recheck = await mysql_authoritative_recheck(
            session=session, chunk_public_ids=["ick_filtered"], user_id=1,
            workspace_key="conversation:conv_1", lexical_channel=True, vector_channel=False,
            allowed_source_types=["artifact"], allowed_source_public_ids=["artifact_old"],
            allowed_source_versions={"artifact_old": "2"},
        )

    assert recheck.approved == []
    assert recheck.rejected_by_chunk == {"ick_filtered": "stale_version"}


async def test_executor_uses_candidate_limit_before_rerank_and_final_limit_afterward(
    monkeypatch,
):
    """A reranker must see more candidates than the final chat context receives."""
    from app.context_engine.indexing.stores_protocol import RetrievedChunk
    from app.context_engine.providers.protocols import RerankResult, RerankScore
    from app.context_engine.retrieval.executor import RetrievalExecutor
    from app.context_engine.retrieval.retrieval import RecheckResult

    observed: dict[str, int] = {}

    class _LexicalStore:
        enabled = True

        async def search(self, **kwargs):
            observed["search_top_k"] = kwargs["top_k"]
            return [
                RetrievedChunk(
                    chunk_public_id=f"chunk-{index}",
                    document_public_id="doc-1",
                    user_id=1,
                    workspace_key="ws1",
                    score=1.0 - index / 100,
                    channel="lexical",
                    rank=index + 1,
                )
                for index in range(kwargs["top_k"])
            ]

    class _Reranker:
        enabled = True

        async def rerank(self, request):
            observed["rerank_items"] = len(request.items)
            return RerankResult(
                request_id=request.request_id,
                model="test-reranker",
                scores=[
                    RerankScore(
                        doc_id=item.doc_id,
                        score=float(item.doc_id.rsplit("-", 1)[1]),
                    )
                    for item in request.items
                ],
            )

    async def _recheck(**kwargs):
        return RecheckResult(
            approved=[
                {
                    "chunk_public_id": f"chunk-{index}",
                    "document_public_id": "doc-1",
                    "user_id": 1,
                    "workspace_key": "ws1",
                    "content": f"content-{index}",
                }
                for index in range(15)
            ]
        )

    async def _audit(**kwargs):
        return "run-1"

    monkeypatch.setattr(
        "app.context_engine.retrieval.executor.mysql_authoritative_recheck",
        _recheck,
    )
    executor = RetrievalExecutor(
        lexical_store=_LexicalStore(),
        lexical_namespace="lexical-test",
        rerank_service=_Reranker(),
    )
    monkeypatch.setattr(executor, "_audit", _audit)
    request = RetrievalRequest(
        query_text="login and sms verification rules",
        source_family="knowledge",
        top_k=5,
        max_candidates=15,
        requested_final_limit=5,
        rerank_strategy=RerankStrategy.RERANKER,
        scope=RetrievalScopeFilter(mode="union", user_id=1, workspace_key="ws1"),
    )

    results, run_id = await executor.execute(
        request=request,
        session=None,
        user_internal_id=1,
        workspace_key="ws1",
        lexical_allowed=True,
        vector_allowed=False,
        rerank_allowed=True,
    )

    assert observed == {"search_top_k": 15, "rerank_items": 15}
    assert [item.chunk_public_id for item in results] == [
        "chunk-14",
        "chunk-13",
        "chunk-12",
        "chunk-11",
        "chunk-10",
    ]
    assert run_id == "run-1"


async def test_executor_excludes_non_evidentiary_candidates_before_rerank(
    monkeypatch,
):
    """Structural and self-disqualified text must not displace project facts."""
    from app.context_engine.indexing.stores_protocol import RetrievedChunk
    from app.context_engine.providers.protocols import RerankResult, RerankScore
    from app.context_engine.retrieval.executor import RetrievalExecutor
    from app.context_engine.retrieval.retrieval import RecheckResult

    content_by_id = {
        "heading-browser": "<section:01_当前需求说明.md>## 6. 浏览器兼容",
        "heading-database": "<section:01_当前需求说明.md>## 7. 数据库",
        "request-label": "<section:02_API规格.md>请求：",
        "irrelevant-menu": (
            "<section:06_无关食堂菜单.md>本文与统一身份认证、登录、测试方案、项目版本、"
            "数据库、安全规范完全无关。在登录模块上下文中不应被选中。"
        ),
        "explicit-negative": (
            "<section:02_API规格.md>本文没有定义 PostgreSQL、Firefox、3 次失败锁定或 60 分钟锁定规则。"
        ),
        "version": "<section:01_当前需求说明.md>当前产品版本：v3.2。",
        "database": "<section:01_当前需求说明.md>生产数据库：MySQL 8.0。",
        "lock": "<section:01_当前需求说明.md>同一账号连续登录失败 5 次后锁定，锁定时长为 30 分钟。",
        "browser": "<section:01_当前需求说明.md>当前支持 Chrome 126 及以上和 Edge 126 及以上。",
    }
    observed: dict[str, list[str]] = {}

    class _LexicalStore:
        enabled = True

        async def search(self, **kwargs):
            return [
                RetrievedChunk(
                    chunk_public_id=chunk_id,
                    document_public_id="doc-1",
                    user_id=1,
                    workspace_key="ws1",
                    score=1.0,
                    channel="lexical",
                    rank=index,
                )
                for index, chunk_id in enumerate(content_by_id, start=1)
            ]

    class _Reranker:
        enabled = True

        async def rerank(self, request):
            observed["rerank_items"] = [item.doc_id for item in request.items]
            scores = {
                "heading-browser": 1.0,
                "heading-database": 0.99,
                "request-label": 0.98,
                "irrelevant-menu": 0.975,
                "explicit-negative": 0.974,
                "version": 0.97,
                "database": 0.96,
                "lock": 0.95,
                "browser": 0.94,
            }
            return RerankResult(
                request_id=request.request_id,
                model="test-reranker",
                scores=[RerankScore(doc_id=item.doc_id, score=scores[item.doc_id]) for item in request.items],
            )

    async def _recheck(**kwargs):
        return RecheckResult(
            approved=[
                {
                    "chunk_public_id": chunk_id,
                    "document_public_id": "doc-1",
                    "user_id": 1,
                    "workspace_key": "ws1",
                    "content": content,
                }
                for chunk_id, content in content_by_id.items()
            ]
        )

    audit_capture: dict = {}

    async def _audit(**kwargs):
        audit_capture.update(kwargs)
        return "run-1"

    monkeypatch.setattr(
        "app.context_engine.retrieval.executor.mysql_authoritative_recheck",
        _recheck,
    )
    executor = RetrievalExecutor(
        lexical_store=_LexicalStore(),
        lexical_namespace="lexical-test",
        rerank_service=_Reranker(),
    )
    monkeypatch.setattr(executor, "_audit", _audit)
    request = RetrievalRequest(
        query_text="current version database lock policy browser compatibility",
        source_family="knowledge",
        top_k=5,
        max_candidates=9,
        requested_final_limit=5,
        rerank_strategy=RerankStrategy.RERANKER,
        scope=RetrievalScopeFilter(mode="union", user_id=1, workspace_key="ws1"),
    )

    results, _ = await executor.execute(
        request=request,
        session=None,
        user_internal_id=1,
        workspace_key="ws1",
        lexical_allowed=True,
        vector_allowed=False,
        rerank_allowed=True,
    )

    assert observed["rerank_items"] == [
        "explicit-negative",
        "version",
        "database",
        "lock",
        "browser",
    ]
    assert [item.chunk_public_id for item in results] == [
        "explicit-negative",
        "version",
        "database",
        "lock",
        "browser",
    ]
    # Observability-only regression: the filtering decision is recorded but
    # the existing selected result set above is unchanged.
    candidates = audit_capture["approved"]
    assert candidates["heading-browser"]["drop_reason"] == "non_evidentiary"
    assert candidates["request-label"]["drop_reason"] == "non_evidentiary"
    assert candidates["heading-browser"]["raw_rank"] == 1
    assert candidates["heading-browser"]["channels"] == ["lexical"]
    assert candidates["heading-browser"]["channel_ranks"] == {"lexical": 1}
