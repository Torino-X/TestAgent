"""生产 Context Runtime 唯一构造链（CE-04 整改 §二）。

组装并仅在此处构造生产组件，杜绝在 Node 内临时构建：

    ModelConfig/ProviderFactory
      → ContextEngine（build_context_engine + Preflight）
      → ContextAwareLLMInvoker
      → ContextInvokerBridge
      → ProductionRuntimeContextFactory → RuntimeContext → LangGraph Node

构造失败时显式 degraded/error，绝不伪造 available（credential 不进日志/State）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge
from app.agent_runtime.context.llm_invoker import ContextAwareLLMInvoker
from app.agent_runtime.context.retry_policy import RetryPolicy
from app.context_engine.models.snapshot_models import (
    ContextStateRef,
    ContextStateStats,
)
from app.context_engine.runtime import build_context_engine
from app.context_engine.snapshot.snapshot_service import ContextSnapshotWriterService


logger = logging.getLogger(__name__)


def build_production_context_components(
    *,
    session_factory: Callable[[], Any],
    settings_service_factory: Callable[[], Any],
    snapshot_repository_factory: Callable[[Any], Any] | None = None,
    preflight=None,
    retry_policy: RetryPolicy | None = None,
    clock: Callable = datetime.utcnow,
    engine_kwargs: dict | None = None,
) -> dict[str, Any]:
    """构造生产 Context Engine / Invoker / Bridge。

    返回 ``{"context_engine": ..., "context_llm_invoker": ...,
    "context_llm_bridge": ..., "error": ...}``。任何一步失败 → 该组件为 None
    并记录 error，调用方按 degraded 处理，不阻塞启动。
    """
    result: dict[str, Any] = {
        "context_engine": None,
        "context_llm_invoker": None,
        "context_llm_bridge": None,
        "error": None,
    }

    try:
        # 1. ContextEngine（build_context_engine 组装默认组件 + Preflight）
        # WP-BE-09：生产链路注册全部 Source Adapter（此前为空注册表，compose
        # 会因 adapter_not_found 失败）。Knowledge executor 可选注入。
        from app.context_engine.feature_flags import get_context_engine_flags
        from app.context_engine.planning.token_counter import TokenCounter
        from app.context_engine.sources.production_registry import (
            build_default_source_registry,
        )

        _flags = get_context_engine_flags()
        if preflight is None:
            from app.context_engine.compression.agent_loop_compactor import AgentLoopCompactor
            from app.context_engine.compression.conversation_compactor import ConversationCompactor
            from app.context_engine.compression.full_replace_compactor import FullReplaceCompactor
            from app.context_engine.compression.preflight_service import ContextPreflightService

            compaction_enabled = bool(
                _flags.context_engine_enabled and _flags.context_compaction_enabled
            )
            preflight = ContextPreflightService(
                enabled=compaction_enabled,
                conversation_compactor=(
                    ConversationCompactor(session_factory=session_factory)
                    if compaction_enabled and _flags.context_conversation_compaction_enabled
                    else None
                ),
                agent_loop_compactor=(
                    AgentLoopCompactor(session_factory=session_factory)
                    if compaction_enabled and _flags.context_agent_loop_compaction_enabled
                    else None
                ),
                full_replace_compactor=(
                    FullReplaceCompactor(session_factory=session_factory)
                    if compaction_enabled and _flags.context_full_replace_enabled
                    else None
                ),
                full_replace_enabled=bool(
                    compaction_enabled and _flags.context_full_replace_enabled
                ),
            )
        knowledge_executor = None
        # Build retrieval infrastructure while the engine master is on. The
        # task-frozen/process retrieval flags are resolved per invocation by
        # KnowledgeSourceAdapter, so a restart cannot erase frozen semantics.
        retrieval_enabled = bool(_flags.context_engine_enabled)
        if retrieval_enabled:
            try:
                from app.context_engine.retrieval.executor import RetrievalExecutor
                from app.context_engine.indexing.vector_store import QdrantVectorStore
                from app.context_engine.indexing.lexical import ElasticsearchLexicalStore
                from app.context_engine.providers.env_provider import (
                    build_env_embedding_provider,
                    build_env_reranker_provider,
                )
                from app.context_engine.providers.runtime_env import (
                    get_context_engine_runtime_env,
                )

                runtime_env = get_context_engine_runtime_env()

                # 从 .env 组装双通道（与 IndexWorker 同 namespace identity）
                qdrant_host = runtime_env.get("QDRANT_HOST", "").strip()
                es_host = runtime_env.get("ES_HOST", "").strip()
                vector_store = (
                    QdrantVectorStore(
                        host=qdrant_host,
                        port=int(runtime_env.get("QDRANT_PORT", "6333") or 6333),
                        api_key=runtime_env.get("QDRANT_API_KEY") or None,
                        https=runtime_env.get("QDRANT_HTTPS", "false").lower() == "true",
                        prefer_grpc=runtime_env.get("QDRANT_PREFER_GRPC", "false").strip().lower() in ("1", "true", "yes"),
                        check_compatibility=False,
                    )
                    if qdrant_host
                    else None
                )
                lexical_store = (
                    ElasticsearchLexicalStore(
                        host=es_host,
                        port=int(runtime_env.get("ES_PORT", "9200") or 9200),
                        user=runtime_env.get("ELASTIC_USER", "elastic"),
                        password=runtime_env.get("ELASTIC_PASSWORD", ""),
                        https=runtime_env.get("ES_HTTPS", "false").lower() == "true",
                    )
                    if es_host
                    else None
                )
                embedding = build_env_embedding_provider(runtime_env)
                embedding_provider = embedding[0] if embedding else None
                embedding_cap = embedding[1] if embedding else None
                embedding_model = embedding_cap.model_name if embedding_cap else None
                embedding_dim = embedding_cap.embedding_dimension if embedding_cap else None
                embedding_ns = None
                lexical_ns = None
                reranker = build_env_reranker_provider(runtime_env)
                rerank_service = reranker[0] if reranker else None
                from app.context_engine.indexing.document_service import CHUNK_POLICY_KEY
                from app.context_engine.indexing.worker_factory import build_index_namespaces

                namespaces = build_index_namespaces(
                    embedding_model=embedding_model,
                    embedding_dimension=embedding_dim,
                    embedding_normalize=runtime_env.get("EMBEDDING_NORMALIZE", "true").lower()
                    in ("1", "true", "yes"),
                    chunk_policy_key=CHUNK_POLICY_KEY,
                )
                embedding_ns = namespaces.vector
                lexical_ns = namespaces.lexical
                knowledge_executor = RetrievalExecutor(
                    vector_store=vector_store,
                    lexical_store=lexical_store,
                    embedding_provider=embedding_provider,
                    embedding_dimension=embedding_dim,
                    embedding_model=embedding_model,
                    embedding_namespace=embedding_ns,
                    lexical_namespace=lexical_ns,
                    rerank_service=rerank_service,
                )
                logger.info(
                    "DIAG | knowledge_executor 构造成功 | "
                    "vector_store=%s | lexical_store=%s | "
                    "embedding_provider=%s | embedding_dim=%s | "
                    "embedding_model=%s | embedding_ns=%s | lexical_ns=%s | reranker=%s",
                    vector_store is not None,
                    lexical_store is not None,
                    embedding_provider is not None,
                    embedding_dim,
                    embedding_model,
                    embedding_ns,
                    lexical_ns,
                    rerank_service is not None,
                )
            except Exception as exc:  # noqa: BLE001 — 检索不可用不影响主链路
                logger.warning(
                    "DIAG | knowledge_executor 构建失败（检索降级）| err=%s | detail=%s",
                    type(exc).__name__,
                    str(exc)[:300],
                )
                knowledge_executor = None

        source_registry = build_default_source_registry(
            token_counter=TokenCounter(),
            knowledge_executor=knowledge_executor,
            retrieval_enabled=retrieval_enabled,
            memory_top_k=5,
        )

        # 2. Snapshot Writer Service（Invoker 生命周期写入；engine 也需要它，
        #   否则 compose 的 _begin_and_ready 因 None writer 崩溃 —— WP-BE-09）。
        def _snapshot_writer_factory() -> ContextSnapshotWriterService:
            repo_factory = snapshot_repository_factory
            if repo_factory is None:
                def _default_repo_factory(session):
                    from app.repositories.context_snapshot_repository import (
                        ContextSnapshotRepository,
                    )
                    return ContextSnapshotRepository(session)

                repo_factory = _default_repo_factory
            return ContextSnapshotWriterService(repo_factory)

        snapshot_writer = _snapshot_writer_factory()

        engine = build_context_engine(
            source_registry=source_registry,
            snapshot_writer=snapshot_writer,
            preflight=preflight,
            **(engine_kwargs or {}),
        )

        # 3. ContextAwareLLMInvoker（Provider 经 settings 构建，事务不跨 LLM）
        # Invoker 的 factory 从 runtime_context 读取已构建好的 llm_client，
        # 避免二次解析（生产 RuntimeContext 每次 invoke 都会重建 llm_client）。
        def _llm_client_factory(runtime_context) -> Any:
            return getattr(runtime_context, "llm_client", None)

        invoker = ContextAwareLLMInvoker(
            engine=engine,
            snapshot_writer=snapshot_writer,
            llm_client_factory=_llm_client_factory,
            retry_policy=retry_policy or RetryPolicy(),
        )

        # 4. ContextInvokerBridge（State-safe：返回 patch，不修改业务 State）
        bridge = ContextInvokerBridge(
            invoker=invoker,
            state_ref_builder=_ProductionStateRefBuilder(),
        )
        # WP-BE-09：Context Engine 总开关开启时启用 bridge（此前默认 enabled=False，
        # 生产 MIG_CHAT=true 会恒 degrade 到 CLARIFY）。Agent 开关独立控制。
        bridge.set_enabled(
            bool(_flags.context_engine_enabled)
        )

        result["context_engine"] = engine
        result["context_llm_invoker"] = invoker
        result["context_llm_bridge"] = bridge
    except Exception as exc:  # noqa: BLE001 — 构造失败不阻塞启动
        logger.warning(
            "Context Runtime 构造失败（degraded）| err=%s | %s",
            type(exc).__name__,
            str(exc)[:160],
        )
        result["error"] = str(type(exc).__name__)

    return result


class _ProductionStateRefBuilder:
    """把 Snapshot 引用组装为轻量 ContextStateRef（≤20KB，不携带内容）。"""

    def __call__(
        self,
        *,
        snapshot_public_id: str,
        profile_key: str,
        token_usage=None,
        source_refs=None,
        stats=None,
    ) -> ContextStateRef:
        usage = token_usage or {}
        stats_obj = stats or ContextStateStats(
            included_ref_count=len(source_refs or []),
            dropped_ref_count=0,
            retrieval_run_count=0,
            estimated_input_tokens=int(usage.get("input", 0) or 0),
            degraded=False,
        )
        return ContextStateRef(
            latest_snapshot_public_id=snapshot_public_id,
            profile_key=profile_key,
            stats=stats_obj,
            source_refs=source_refs or [],
        )


__all__ = ["build_production_context_components", "_ProductionStateRefBuilder"]


# 模块定位:生产 Context Runtime 唯一构造链(CE-04 整改 §二)
#
# 组装并**仅在此处**构造生产组件,杜绝在 Node 内临时构建:
#   ModelConfig / ProviderFactory → ContextEngine → Preflight
#     → ToolInvoker / RetrievalPlan → CacheLayer → EventSink
#
# 链路:
#   startup → production_runtime_context_factory.build()
#     → context_runtime_builder.assemble()
#       → 各组件单例化 → 注入 LangGraphRunCoordinator
#
# 关键约束:
#   - 构造顺序不能乱(Preflight 必须在 ContextEngine 之后);
#   - 不要在节点内部现拉实例(每次都新建 LLMClient 是禁止的);
#   - 替换实现要换 assembler,不直接改 Coordinator。
