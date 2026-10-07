"""CE-03 Feature Flag 测试：19 项默认关闭 + 依赖矩阵 + disabled/on 行为。

覆盖：
- 19 flags 全默认 False；
- 依赖矩阵：retrieval 依赖 engine；lexical 依赖 retrieval；dense 依赖 retrieval；
  memory read 依赖 engine；
- disabled 时检索管线不产生行为差异（Knowledge adapter 返回确定性空 + warning）。
"""

from __future__ import annotations

import pytest

from app.context_engine.feature_flags import (
    ContextEngineFeatureFlags,
    ContextFeatureFlag,
)


def test_all_19_flags_default_false():
    flags = ContextEngineFeatureFlags()
    assert len(flags.__dataclass_fields__) == len(ContextFeatureFlag)
    for field_name, field in flags.__dataclass_fields__.items():
        if field.default is False:
            assert getattr(flags, field_name) is False


def test_dependency_matrix():
    # retrieval 依赖 engine
    assert ContextEngineFeatureFlags(context_engine_enabled=False, context_retrieval_enabled=True).retrieval_implies_engine is False
    assert ContextEngineFeatureFlags(context_engine_enabled=True, context_retrieval_enabled=True).retrieval_implies_engine is True
    # lexical 依赖 retrieval
    assert ContextEngineFeatureFlags(context_engine_enabled=True, context_retrieval_enabled=True, context_lexical_retrieval_enabled=True).lexical_implies_retrieval is True
    assert ContextEngineFeatureFlags(context_engine_enabled=True, context_retrieval_enabled=False, context_lexical_retrieval_enabled=True).lexical_implies_retrieval is False
    # dense 依赖 retrieval
    assert ContextEngineFeatureFlags(context_engine_enabled=True, context_retrieval_enabled=True, context_dense_retrieval_enabled=True).dense_requires_retrieval is True
    assert ContextEngineFeatureFlags(context_engine_enabled=True, context_retrieval_enabled=False, context_dense_retrieval_enabled=True).dense_requires_retrieval is False
    # memory read 依赖 engine
    assert ContextEngineFeatureFlags(context_engine_enabled=False, context_memory_read_enabled=True).memory_read_implies_engine is False
    assert ContextEngineFeatureFlags(context_engine_enabled=True, context_memory_read_enabled=True).memory_read_implies_engine is True


async def test_knowledge_adapter_disabled_deterministic_empty(sqlite_session_factory):
    """disabled 时 Knowledge adapter 返回确定性空 + warning（不伪造）。"""
    from app.context_engine.sources.knowledge import KnowledgeSourceAdapter
    from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
    from app.context_engine.models.enums import ContextKind

    adapter = KnowledgeSourceAdapter(executor=None, retrieval_enabled=False)
    request = ContextRequest(user_id="usr_1", call_site="x")
    scope = ContextScope(user_id="usr_1", thread_id="t1")
    section = SectionPlan(kind=ContextKind.KNOWLEDGE, required=False, budget_tokens=1000)
    result = await adapter.collect(request, section, scope, runtime_context=None)
    assert result.degraded is True
    assert result.item_count == 0
    assert any(w.code == "context.source.knowledge_not_enabled" for w in result.warnings)


async def test_knowledge_adapter_collects_maas_runtime_result_when_retrieval_disabled():
    """Maas KB results must enter the CE knowledge section, not a legacy prompt splice."""
    from types import SimpleNamespace

    from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
    from app.context_engine.models.enums import ContextKind
    from app.context_engine.sources.knowledge import KnowledgeSourceAdapter

    adapter = KnowledgeSourceAdapter(executor=None, retrieval_enabled=False)
    request = ContextRequest(user_id="usr_1", call_site="test_plan.generate.outline")
    scope = ContextScope(user_id="usr_1", thread_id="t1")
    section = SectionPlan(kind=ContextKind.KNOWLEDGE, required=False, budget_tokens=1000)
    runtime_context = SimpleNamespace(
        knowledge_search_result={
            "source": "company_rag",
            "query": "payment idempotency testing rules",
            "hit_count": 1,
            "degraded": False,
            "similar_projects": [
                {
                    "name": "payment rules",
                    "snippet": "UNIQUE_MAAS_SNIPPET: retry requests must be idempotent.",
                }
            ],
        }
    )

    result = await adapter.collect(
        request,
        section,
        scope,
        runtime_context=runtime_context,
    )

    assert result.degraded is False
    assert result.item_count == 1
    assert result.items[0].kind == ContextKind.KNOWLEDGE
    assert "UNIQUE_MAAS_SNIPPET" in result.items[0].content


async def test_knowledge_adapter_truncates_oversized_on_demand_query(sqlite_session_factory):
    """Knowledge adapter should not let long chat context violate RetrievalRequest."""
    from types import SimpleNamespace

    from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
    from app.context_engine.models.enums import ContextKind
    from app.context_engine.sources.knowledge import KnowledgeSourceAdapter

    captured = {}

    class _Executor:
        async def execute(self, *, request, session, user_internal_id, workspace_key, agent_type, **kwargs):
            captured["query_text"] = request.query_text
            return [], "run-1"

    adapter = KnowledgeSourceAdapter(executor=_Executor(), retrieval_enabled=True)
    request = ContextRequest(
        user_id="1",
        call_site="chat.reply",
        current_user_message="问" * 2500,
    )
    scope = ContextScope(user_id="1", thread_id="t1")
    section = SectionPlan(kind=ContextKind.KNOWLEDGE, required=False, budget_tokens=1000)
    runtime_context = SimpleNamespace(
        session_factory=sqlite_session_factory,
        user_internal_id=1,
    )

    result = await adapter.collect(request, section, scope, runtime_context=runtime_context)

    assert result.degraded is True
    assert captured["query_text"] == "问" * 2000


async def test_knowledge_adapter_expands_rerank_candidates_but_keeps_final_limit(
    sqlite_session_factory,
):
    """Reranking needs a larger candidate pool than the final chat context."""
    from types import SimpleNamespace

    from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
    from app.context_engine.models.enums import ContextKind
    from app.context_engine.sources.knowledge import KnowledgeSourceAdapter

    captured = {}

    class _Executor:
        async def execute(self, *, request, session, user_internal_id, workspace_key, agent_type, **kwargs):
            captured["request"] = request
            return [], "run-1"

    adapter = KnowledgeSourceAdapter(executor=_Executor(), retrieval_enabled=True, top_k=5)
    request = ContextRequest(
        user_id="1",
        call_site="chat.reply",
        current_user_message="current login module rules",
    )
    scope = ContextScope(user_id="1", thread_id="t1", workspace_key="ws1")
    section = SectionPlan(kind=ContextKind.KNOWLEDGE, required=False, budget_tokens=1000)
    runtime_context = SimpleNamespace(
        session_factory=sqlite_session_factory,
        user_internal_id=1,
    )

    await adapter.collect(request, section, scope, runtime_context=runtime_context)

    retrieval_request = captured["request"]
    assert retrieval_request.top_k == 5
    assert retrieval_request.max_candidates == 15
    assert retrieval_request.requested_final_limit == 5


def test_evaluate_returns_flag_values():
    flags = ContextEngineFeatureFlags(context_retrieval_enabled=True)
    assert flags.evaluate(ContextFeatureFlag.CONTEXT_RETRIEVAL_ENABLED) is True
    assert flags.evaluate(ContextFeatureFlag.CONTEXT_DENSE_RETRIEVAL_ENABLED) is False


def test_production_registry_reserves_seven_knowledge_slots_for_multi_fact_answers():
    """The chat evidence budget must accommodate multi-fact project questions."""
    from app.context_engine.models.enums import ContextKind
    from app.context_engine.sources.production_registry import (
        DEFAULT_KNOWLEDGE_FINAL_LIMIT,
        build_default_source_registry,
    )

    registry = build_default_source_registry(retrieval_enabled=False)
    adapter = registry.get_for_kind(ContextKind.KNOWLEDGE)

    assert DEFAULT_KNOWLEDGE_FINAL_LIMIT == 7
    assert adapter._top_k == DEFAULT_KNOWLEDGE_FINAL_LIMIT
