"""生产 IndexWorker 工厂 — 从 .env 组装 IndexWorker + ProductionIndexJobHandler。

WP-BE-01：main.py lifespan 调用本工厂构建并启动 IndexWorker。

从环境变量构造：
- Embedding Provider（EMBEDDING_*，缺配置 → dense disabled/skipped）
- Qdrant（QDRANT_*，缺库 → 检索通道降级）
- Elasticsearch（ES_*/ELASTIC_*，缺库 → 词法通道降级）

namespace identity 与 RetrievalExecutor 一致（build_namespace_identity +
vector_namespace_name / lexical_namespace_name），保证写与读使用同一外部索引。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.context_engine.indexing.production_handler import ProductionIndexJobHandler
from app.context_engine.indexing.stores_protocol import (
    build_namespace_identity,
    lexical_namespace_name,
    vector_namespace_name,
)
from app.context_engine.providers.runtime_env import get_context_engine_runtime_env

logger = logging.getLogger(__name__)

_DEFAULT_CHUNK_POLICY_KEY = "recursive_char:v1"


@dataclass(frozen=True)
class IndexNamespaces:
    vector: str | None
    lexical: str


def build_index_namespaces(
    *,
    embedding_model: str | None,
    embedding_dimension: int | None,
    embedding_normalize: bool,
    chunk_policy_key: str = _DEFAULT_CHUNK_POLICY_KEY,
) -> IndexNamespaces:
    """Build stable channel namespaces without coupling lexical to dense."""
    lexical_identity = build_namespace_identity(
        provider_type="lexical",
        provider_config_public_id="env_elasticsearch",
        model="bm25",
        dimension=0,
        normalize=False,
        chunk_policy_key=chunk_policy_key,
    )
    vector_namespace = None
    if embedding_dimension and embedding_model:
        vector_identity = build_namespace_identity(
            provider_type="embedding",
            provider_config_public_id="env_embedding",
            model=embedding_model,
            dimension=embedding_dimension,
            normalize=embedding_normalize,
            chunk_policy_key=chunk_policy_key,
        )
        vector_namespace = vector_namespace_name(vector_identity)
    return IndexNamespaces(
        vector=vector_namespace,
        lexical=lexical_namespace_name(lexical_identity),
    )


def _env_str(key: str, default: str = "", *, env: dict[str, str] | None = None) -> str:
    return (env or get_context_engine_runtime_env()).get(key, default).strip()


def _env_bool(key: str, default: bool = False, *, env: dict[str, str] | None = None) -> bool:
    value = _env_str(key, env=env).lower()
    if not value:
        return default
    return value in {"1", "true", "yes"}


def build_production_index_worker(
    *,
    session_factory,
    poll_interval_seconds: float = 1.0,
    lease_seconds: int = 60,
    chunk_policy_key: str = _DEFAULT_CHUNK_POLICY_KEY,
) -> Any:
    """构造生产 IndexWorker（含 channel handler）。

    返回 IndexWorker；任何依赖缺失时仍返回 Worker，channel job 按
    skipped / degraded 语义处理（不伪造可用）。
    """
    from app.context_engine.indexing.index_worker import IndexWorker
    from app.context_engine.providers.env_provider import (
        build_env_embedding_provider,
    )
    from app.context_engine.indexing.vector_store import QdrantVectorStore
    from app.context_engine.indexing.lexical import ElasticsearchLexicalStore

    env = get_context_engine_runtime_env()

    # ── Store（缺库 → enabled=False，检索/写入通道降级）────────────
    vector_store = None
    lexical_store = None
    try:
        qdrant_host = _env_str("QDRANT_HOST", env=env)
        if qdrant_host:
            vector_store = QdrantVectorStore(
                host=qdrant_host,
                port=int(_env_str("QDRANT_PORT", "6333", env=env) or 6333),
                api_key=_env_str("QDRANT_API_KEY", env=env) or None,
                https=_env_bool("QDRANT_HTTPS", False, env=env),
                prefer_grpc=_env_bool("QDRANT_PREFER_GRPC", False, env=env),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Qdrant store 构造失败（dense 通道降级）| err=%s", exc)
        vector_store = None

    try:
        es_host = _env_str("ES_HOST", env=env)
        if es_host:
            lexical_store = ElasticsearchLexicalStore(
                host=es_host,
                port=int(_env_str("ES_PORT", "9200", env=env) or 9200),
                user=_env_str("ELASTIC_USER", "elastic", env=env),
                password=_env_str("ELASTIC_PASSWORD", env=env),
                https=_env_bool("ES_HTTPS", False, env=env),
                verify_certs=_env_bool("ES_HTTPS", False, env=env),
                ca_certs=_env_str("ES_CA_CERTS", env=env) or None,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Elasticsearch store 构造失败（lexical 通道降级）| err=%s", exc)
        lexical_store = None

    # ── Embedding Provider（缺配置 → dense skipped）────────────────
    embedding_provider = None
    embedding_model = None
    embedding_dimension = None
    try:
        built = build_env_embedding_provider(env)
        if built is not None:
            provider, capability, _ = built
            embedding_provider = provider
            embedding_model = capability.model_name
            embedding_dimension = capability.embedding_dimension
    except Exception as exc:  # noqa: BLE001
        logger.warning("Embedding Provider 构造失败（dense 通道降级）| err=%s", exc)

    # ── namespace（与 RetrievalExecutor 一致）──────────────────────
    embedding_namespace = None
    lexical_namespace = None
    try:
        namespaces = build_index_namespaces(
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
            embedding_normalize=_env_bool("EMBEDDING_NORMALIZE", True, env=env),
            chunk_policy_key=chunk_policy_key,
        )
        embedding_namespace = namespaces.vector
        lexical_namespace = namespaces.lexical
    except Exception as exc:  # noqa: BLE001
        logger.warning("namespace identity 解析失败 | err=%s", exc)

    handler = ProductionIndexJobHandler(
        vector_store=vector_store,
        lexical_store=lexical_store,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        embedding_dimension=embedding_dimension,
    )

    worker = IndexWorker(
        session_factory=session_factory,
        handler=handler,
        embedding_namespace=embedding_namespace,
        lexical_namespace=lexical_namespace,
        chunk_policy_key=chunk_policy_key,
        poll_interval_seconds=poll_interval_seconds,
        lease_seconds=lease_seconds,
    )
    logger.info(
        "Production IndexWorker built | dense=%s lexical=%s embedding=%s | "
        "embedding_namespace=%s lexical_namespace=%s",
        bool(vector_store is not None and getattr(vector_store, "enabled", False)),
        bool(lexical_store is not None and getattr(lexical_store, "enabled", False)),
        embedding_provider is not None,
        embedding_namespace,
        lexical_namespace,
    )
    return worker


__all__ = ["IndexNamespaces", "build_index_namespaces", "build_production_index_worker"]
# auto-appended module-level note: index worker 工厂: 按配置造 IndexWorker 实例。
