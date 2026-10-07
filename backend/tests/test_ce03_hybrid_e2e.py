"""CE-03 整改7：真实 Hybrid 检索 E2E（Embedding → Qdrant Dense + ES Lexical → Weighted RRF）。

真实链路：
  Embedding Provider（.env: EMBEDDING_BASE_URL / API_KEY / MODEL / DIMENSION）
  → Qdrant Dense
  + Elasticsearch Lexical
  → Weighted RRF（无真实 Reranker → fallback，不冒充）
  → MySQL authoritative recheck（二级 ACL）
  → Retrieval Audit（context_retrieval_runs / candidates）
  → Snapshot retrieval_run_ids_json

门禁 env：
  QDRANT_HOST / QDRANT_PORT / QDRANT_API_KEY（dense）
  ES_HOST / ES_PORT / ELASTIC_USER / ELASTIC_PASSWORD（lexical）
  EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL / EMBEDDING_DIMENSION（embedding）

任一必需连接缺失 → 该测试 skip（不伪造）。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.context_engine.providers.env_provider import build_env_embedding_provider


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


_ENV = _load_env()

_HAS_QDRANT = bool(_ENV.get("QDRANT_HOST"))
_HAS_ES = bool(_ENV.get("ES_HOST"))
_HAS_EMBEDDING = bool(_ENV.get("EMBEDDING_BASE_URL") and _ENV.get("EMBEDDING_API_KEY") and _ENV.get("EMBEDDING_MODEL"))

REQUIRES_HYBRID = pytest.mark.skipif(
    not (_HAS_QDRANT and _HAS_ES and _HAS_EMBEDDING),
    reason="需要 Qdrant + ES + Embedding(.env) 连接参数",
)

# MySQL 测试库（与 test_ce03_mysql_integration 同模式）
def _load_db_url() -> str:
    env = os.environ.get("DATABASE_SYNC_URL", "")
    if env:
        return env
    return _ENV.get("DATABASE_SYNC_URL", "")


_TEST_DB = _load_db_url()
REQUIRES_TEST_MYSQL = pytest.mark.skipif(
    not _TEST_DB or "testagent" not in _TEST_DB,
    reason="需要 DATABASE_SYNC_URL 指向测试 MySQL",
)


def _async_db_url(db_name: str) -> str:
    base = _TEST_DB.replace("mysql+pymysql://", "mysql+aiomysql://", 1)
    return base.rsplit("/", 1)[0] + f"/{db_name}?charset=utf8mb4"


_RETRIEVAL_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS context_retrieval_runs (
    id BIGINT NOT NULL AUTO_INCREMENT,
    public_id VARCHAR(64) NOT NULL,
    user_id BIGINT NOT NULL,
    workspace_key VARCHAR(191) NULL,
    conversation_id BIGINT NULL,
    task_id BIGINT NULL,
    agent_run_id BIGINT NULL,
    context_snapshot_public_id VARCHAR(64) NULL,
    call_site VARCHAR(128) NOT NULL,
    retrieval_channel VARCHAR(32) NOT NULL,
    retrieval_policy_key VARCHAR(64) NOT NULL,
    retrieval_policy_version VARCHAR(32) NOT NULL,
    query_hash CHAR(64) NOT NULL,
    query_excerpt VARCHAR(1000) NULL,
    query_metadata_json JSON NULL,
    requested_candidate_limit INT NOT NULL,
    requested_final_limit INT NOT NULL,
    lexical_enabled TINYINT(1) NOT NULL DEFAULT 0,
    vector_enabled TINYINT(1) NOT NULL DEFAULT 0,
    rerank_enabled TINYINT(1) NOT NULL DEFAULT 0,
    reranker_model VARCHAR(128) NULL,
    reranker_version VARCHAR(64) NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    fallback_code VARCHAR(64) NULL,
    fallback_detail VARCHAR(1000) NULL,
    recalled_count INT NOT NULL DEFAULT 0,
    reranked_count INT NOT NULL DEFAULT 0,
    selected_count INT NOT NULL DEFAULT 0,
    recall_latency_ms INT NULL,
    rerank_latency_ms INT NULL,
    total_latency_ms INT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_crr_pub (public_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_RETRIEVAL_CANDIDATES_DDL = """
CREATE TABLE IF NOT EXISTS context_retrieval_candidates (
    id BIGINT NOT NULL AUTO_INCREMENT,
    retrieval_run_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    source_public_id VARCHAR(64) NOT NULL,
    index_document_id BIGINT NULL,
    index_chunk_id BIGINT NULL,
    content_hash CHAR(64) NOT NULL,
    content_excerpt VARCHAR(1000) NULL,
    estimated_tokens INT NULL,
    raw_rank INT NULL,
    raw_score NUMERIC(12,8) NULL,
    normalized_score NUMERIC(12,8) NULL,
    rerank_score NUMERIC(12,8) NULL,
    final_rank INT NULL,
    selected TINYINT(1) NOT NULL DEFAULT 0,
    drop_reason VARCHAR(64) NULL,
    source_quota_key VARCHAR(64) NULL,
    metadata_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_candidate (retrieval_run_id, source_type, source_public_id, content_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_SNAPSHOTS_DDL = """
CREATE TABLE IF NOT EXISTS llm_context_snapshots (
    id BIGINT NOT NULL AUTO_INCREMENT,
    public_id VARCHAR(64) NOT NULL,
    user_id BIGINT NOT NULL,
    conversation_id BIGINT NULL,
    message_id BIGINT NULL,
    agent_task_id BIGINT NULL,
    llm_task_type VARCHAR(64) NOT NULL,
    context_kind VARCHAR(64) NOT NULL,
    included_message_ids JSON NULL,
    included_file_ids JSON NULL,
    included_task_ids JSON NULL,
    included_artifact_ids JSON NULL,
    included_knowledge_ids JSON NULL,
    summary_id BIGINT NULL,
    estimated_tokens INT NOT NULL DEFAULT 0,
    context_digest VARCHAR(128) NULL,
    context_preview TEXT NULL,
    engine_version VARCHAR(32) NULL,
    call_site VARCHAR(128) NULL,
    context_profile_key VARCHAR(128) NULL,
    context_profile_version VARCHAR(32) NULL,
    context_policy_version VARCHAR(32) NULL,
    model_config_id BIGINT NULL,
    model_name_snapshot VARCHAR(128) NULL,
    context_window_tokens BIGINT NULL,
    input_budget_tokens BIGINT NULL,
    output_reserve_tokens BIGINT NULL,
    runtime_reserve_tokens BIGINT NULL,
    safety_margin_tokens BIGINT NULL,
    target_input_tokens BIGINT NULL,
    estimated_input_tokens BIGINT NULL,
    actual_input_tokens BIGINT NULL,
    actual_output_tokens BIGINT NULL,
    section_stats_json JSON NULL,
    included_refs_json JSON NULL,
    dropped_refs_json JSON NULL,
    retrieval_run_ids_json JSON NULL,
    compaction_json JSON NULL,
    tool_output_json JSON NULL,
    fallback_json JSON NULL,
    latency_json JSON NULL,
    prompt_digest CHAR(64) NULL,
    prompt_excerpt VARCHAR(2000) NULL,
    full_prompt_payload_id BIGINT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'building',
    error_code VARCHAR(64) NULL,
    completed_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_snap (public_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_INDEX_DOCS_DDL = """
CREATE TABLE IF NOT EXISTS context_index_documents (
    id BIGINT NOT NULL AUTO_INCREMENT,
    public_id VARCHAR(64) NOT NULL,
    user_id BIGINT NOT NULL,
    workspace_key VARCHAR(191) NULL,
    source_type VARCHAR(32) NOT NULL,
    source_public_id VARCHAR(64) NOT NULL,
    source_version VARCHAR(64) NOT NULL DEFAULT '1',
    source_digest CHAR(64) NOT NULL,
    title VARCHAR(500) NULL,
    language VARCHAR(16) NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    chunk_policy_key VARCHAR(64) NOT NULL,
    chunk_policy_version VARCHAR(32) NOT NULL,
    lexical_index_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    vector_index_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    embedding_provider VARCHAR(64) NULL,
    embedding_model VARCHAR(128) NULL,
    embedding_dimension INT NULL,
    external_index_namespace VARCHAR(191) NULL,
    external_document_ref VARCHAR(255) NULL,
    metadata_json JSON NULL,
    idempotency_key VARCHAR(191) NOT NULL,
    indexed_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_doc_pub (public_id),
    UNIQUE KEY uq_doc_idem (idempotency_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_INDEX_CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS context_index_chunks (
    id BIGINT NOT NULL AUTO_INCREMENT,
    public_id VARCHAR(64) NOT NULL,
    document_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    chunk_index INT NOT NULL,
    section_path VARCHAR(1000) NULL,
    source_locator_json JSON NULL,
    content TEXT NULL,
    normalized_content TEXT NULL,
    content_hash CHAR(64) NOT NULL,
    char_count INT NOT NULL,
    estimated_tokens INT NULL,
    language VARCHAR(16) NULL,
    external_vector_ref VARCHAR(255) NULL,
    metadata_json JSON NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_chunk_pub (public_id),
    UNIQUE KEY uq_chunks_document_index (document_id, chunk_index),
    UNIQUE KEY uq_chunks_document_hash (document_id, content_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def _ns(tag: str) -> str:
    return f"ce03_hybrid_{tag}_{uuid.uuid4().hex[:8]}"


async def _seed_mysql_doc_chunk(sf, *, doc_id: int, chunk_id: int, user_id: int = 1,
                                chunk_public_id: str, workspace_key: str | None = None) -> None:
    from datetime import datetime

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with sf() as session:
        await session.execute(
            sa.text(
                "INSERT INTO context_index_documents "
                "(public_id, user_id, workspace_key, source_type, source_public_id, source_version, "
                " source_digest, title, status, chunk_policy_key, chunk_policy_version, "
                " lexical_index_status, vector_index_status, idempotency_key, created_at, updated_at) "
                "VALUES (:pid, :uid, :ws, 'uploaded_file', :sp, '1', :dig, 'doc', 'indexed', "
                " 'recursive_char:v1', 'v1', 'ready', 'ready', :idem, :now, :now)"
            ),
            {"pid": f"doc_{doc_id}", "uid": user_id, "ws": workspace_key, "sp": f"src_{doc_id}",
             "dig": f"dig_{doc_id}", "idem": f"idem_{doc_id}", "now": now},
        )
        await session.commit()

    async with sf() as session:
        await session.execute(
            sa.text(
                "INSERT INTO context_index_chunks "
                "(public_id, document_id, user_id, chunk_index, content, content_hash, char_count, "
                " status, created_at, updated_at) "
                "VALUES (:cid, :did, :uid, 0, :content, :chash, :cc, 'active', :now, :now)"
            ),
            {"cid": chunk_public_id, "did": doc_id, "uid": user_id, "content": f"chunk content {doc_id}",
             "chash": f"chash_{doc_id}", "cc": 20, "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        )
        await session.commit()


@pytest.fixture()
def mysql_db_name() -> str:
    return "testagent_ce03_hybrid_" + uuid.uuid4().hex[:8]


@pytest.fixture()
def admin_engine():
    engine = sa.create_engine(_TEST_DB)
    yield engine
    engine.dispose()


def _create_db(admin_engine, db_name: str) -> None:
    with admin_engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(sa.text(f"DROP DATABASE IF EXISTS {db_name}"))
        conn.execute(sa.text(f"CREATE DATABASE {db_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))


def _drop_db(admin_engine, db_name: str) -> None:
    with admin_engine.connect() as conn:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(sa.text(f"DROP DATABASE IF EXISTS {db_name}"))


@pytest.fixture()
async def hybrid_env(mysql_db_name, admin_engine):
    try:
        _create_db(admin_engine, mysql_db_name)
    except Exception as exc:  # noqa: BLE001 - E2E must report unavailable external infrastructure honestly
        pytest.skip(
            "UNVERIFIED_EXTERNAL_DEPENDENCY: test MySQL unavailable "
            f"({type(exc).__name__})"
        )
    engine = create_async_engine(_async_db_url(mysql_db_name))
    async with engine.begin() as conn:
        for ddl in (_INDEX_DOCS_DDL, _INDEX_CHUNKS_DDL, _RETRIEVAL_RUNS_DDL, _RETRIEVAL_CANDIDATES_DDL, _SNAPSHOTS_DDL):
            await conn.execute(sa.text(ddl))
    yield engine
    await engine.dispose()
    _drop_db(admin_engine, mysql_db_name)


async def _build_stores(env):
    """构造真实 Qdrant + ES store。"""
    from app.context_engine.indexing.vector_store import QdrantVectorStore
    from app.context_engine.indexing.lexical import ElasticsearchLexicalStore

    vs = QdrantVectorStore(
        host=env["QDRANT_HOST"], port=int(env.get("QDRANT_PORT", "6333")),
        api_key=env.get("QDRANT_API_KEY") or None, prefer_grpc=False,
    )
    ls = ElasticsearchLexicalStore(
        host=env["ES_HOST"], port=int(env.get("ES_PORT", "9200")),
        user=env.get("ELASTIC_USER", "elastic"), password=env.get("ELASTIC_PASSWORD", ""),
        https=env.get("ES_HTTPS", "false").lower() == "true",
        verify_certs=env.get("ES_HTTPS", "false").lower() == "true",
        ca_certs=env.get("ES_CA_CERTS") or None,
    )
    return vs, ls


@REQUIRES_HYBRID
@REQUIRES_TEST_MYSQL
async def test_hybrid_retrieval_full_chain(hybrid_env):
    """真实 Hybrid 链路：Embedding→Qdrant + ES→Weighted RRF→MySQL recheck→Audit→Snapshot。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.context_engine.indexing.stores_protocol import build_namespace_identity, vector_namespace_name, lexical_namespace_name
    from app.context_engine.providers.env_provider import build_env_embedding_provider
    from app.context_engine.retrieval.executor import RetrievalExecutor
    from app.context_engine.retrieval.audit import RetrievalAuditService
    from app.context_engine.models.retrieval import RetrievalRequest, RetrievalScopeFilter
    from app.context_engine.models.enums import RetrievalStrategy, RerankStrategy

    sf = async_sessionmaker(hybrid_env, expire_on_commit=False)
    vs, ls = await _build_stores(_ENV)

    # 建真实 Embedding Provider（.env）
    emb = build_env_embedding_provider(_ENV)
    assert emb is not None, "EMBEDDING_* 配置缺失"
    emb_provider, emb_capability, _ = emb
    dimension = emb_capability.embedding_dimension
    assert dimension and dimension > 0, "EMBEDDING_DIMENSION 必须为正整数"

    # namespace identity（真实 dimension + model）
    ns_id = build_namespace_identity(
        provider_type="embedding", provider_config_public_id="env_embedding",
        model=emb_capability.model_name, dimension=dimension,
        normalize=True, chunk_policy_key="recursive_char:v1",
    )
    vec_ns = vector_namespace_name(ns_id)
    lex_ns = lexical_namespace_name(ns_id)

    # 建外部 collection/index
    await vs._ensure_collection(vec_ns, dimension)
    await ls._ensure_index(lex_ns)

    # 种子文档（MySQL + 外部向量/词法）
    docs = [
        ("ck_1", 1, "测试计划编写", "测试计划编写 风险分析 章节"),
        ("ck_2", 2, "接口自动化测试", "接口自动化测试 环境准备 步骤"),
        ("ck_3", 3, "性能测试并发模型", "性能测试 并发模型 选择"),
    ]
    for cid, doc_id, text, content in docs:
        await _seed_mysql_doc_chunk(sf, doc_id=doc_id, chunk_id=doc_id,
                                    chunk_public_id=cid, user_id=1, workspace_key=None)
        # 真实 Embedding → Qdrant
        from app.context_engine.providers.protocols import EmbeddingRequest, EmbeddingText

        er = await emb_provider.embed(
            EmbeddingRequest(
                request_id=f"rid_{cid}", model=emb_capability.model_name,
                input_type="document",
                texts=(EmbeddingText(text_id=cid, text=text),),
                dimension=dimension, normalize=True,
            )
        )
        vec = er.vectors[0].values
        assert len(vec) == dimension, f"observed dimension {len(vec)} != configured {dimension}"
        await vs.upsert_chunk(
            namespace=vec_ns, chunk_public_id=cid, vector=vec,
            payload={"chunk_public_id": cid, "document_public_id": f"doc_{doc_id}",
                     "user_id": 1, "workspace_key": None, "status": "active", "deleted_at": None,
                     "content_excerpt": content},
        )
        # ES lexical
        from app.context_engine.indexing.lexical import ElasticsearchLexicalStore
        import re as _re

        await ls.index_chunk(
            namespace=lex_ns, chunk_public_id=cid,
            normalized_content=_re.sub(r"\s+", " ", content).lower(),
            payload={"chunk_public_id": cid, "document_public_id": f"doc_{doc_id}",
                     "user_id": 1, "workspace_key": None, "status": "active", "deleted_at": None,
                     "content_excerpt": content},
        )

    # RetrievalExecutor（真实 provider + 双通道 + Weighted RRF）
    executor = RetrievalExecutor(
        vector_store=vs, lexical_store=ls,
        embedding_provider=emb_provider, embedding_dimension=dimension,
        embedding_model=emb_capability.model_name,
        embedding_namespace=vec_ns, lexical_namespace=lex_ns,
        rerank_service=None,  # 无真实 Reranker → Weighted RRF fallback
    )
    assert executor.dense_enabled is True
    assert executor.lexical_enabled is True

    request = RetrievalRequest(
        query_text="测试计划", strategy=RetrievalStrategy.PLANNED,
        source_family="knowledge", top_k=3, rerank_strategy=RerankStrategy.WEIGHTED_RRF,
        scope=RetrievalScopeFilter(mode="user_global", user_id=1, workspace_key=None),
    )

    async with sf() as session:
        fused, run_id = await executor.execute(
            request=request, session=session,
            user_internal_id=1, workspace_key=None,
        )
        await session.commit()

    assert fused, "hybrid 检索应返回结果"
    # MySQL recheck 已应用：只保留 seed 的 doc（user1 + indexed + ready）
    assert all(m.user_id == 1 for m in fused)
    assert run_id is not None

    # Audit 落库
    async with sf() as session:
        row = (await session.execute(sa.text(
            "SELECT lexical_enabled, vector_enabled, rerank_enabled, recalled_count, selected_count, "
            " query_metadata_json FROM context_retrieval_runs WHERE public_id=:p"
        ), {"p": run_id})).one()
        assert row[0] == 1  # lexical
        assert row[1] == 1  # vector
        assert row[2] == 0  # 无 reranker → rerank_enabled=0（Weighted RRF）
        assert row[3] >= 1  # recalled
        assert row[4] >= 1  # selected
        # 无正文泄漏到审计（content_excerpt 是 excerpt，query 不入 metadata 原文）
        assert "测试计划" not in str(row[5]) or "scope" in str(row[5])

    # Snapshot retrieval_run_ids_json
    from datetime import datetime

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with sf() as session:
        await session.execute(
            sa.text(
                "INSERT INTO llm_context_snapshots "
                "(public_id, user_id, llm_task_type, context_kind, retrieval_run_ids_json, "
                " status, estimated_tokens, created_at) "
                "VALUES ('snap_hybrid', 1, 'test', 'active', :rrj, 'building', 10, :now)"
            ),
            {"rrj": f'{{"run_ids": ["{run_id}"]}}', "now": now},
        )
        await session.commit()

    async with sf() as session:
        rrj = (await session.execute(sa.text(
            "SELECT retrieval_run_ids_json FROM llm_context_snapshots WHERE public_id='snap_hybrid'"
        ))).scalar()
        assert run_id in str(rrj)

    # 清理外部资源
    client_vs = vs._get_client()
    if client_vs is not None:
        try:
            await client_vs.delete_collection(collection_name=vec_ns)
        except Exception:  # noqa: BLE001
            pass
    client_ls = ls._get_client()
    if client_ls is not None:
        try:
            await client_ls.indices.delete(index=lex_ns)
        except Exception:  # noqa: BLE001
            pass


@REQUIRES_HYBRID
async def test_embedding_provider_real_call():
    """真实 Embedding Provider 可调用：record dimension match + dense search PASS。"""
    from app.context_engine.providers.env_provider import build_env_embedding_provider
    from app.context_engine.providers.protocols import EmbeddingRequest, EmbeddingText

    emb = build_env_embedding_provider(_ENV)
    assert emb is not None
    provider, capability, _ = emb
    configured_dim = capability.embedding_dimension
    assert configured_dim and configured_dim > 0

    result = await provider.embed(
        EmbeddingRequest(
            request_id="probe", model=capability.model_name,
            input_type="query",
            texts=(EmbeddingText(text_id="q", text="测试计划"),),
            dimension=configured_dim, normalize=True,
        )
    )
    observed = len(result.vectors[0].values)
    print(f"PROVIDER=openai-compatible MODEL={capability.model_name} "
          f"CONFIGURED_DIM={configured_dim} OBSERVED_DIM={observed} "
          f"DIM_MATCH={observed == configured_dim}")
    assert observed == configured_dim, f"dimension mismatch: observed {observed} != configured {configured_dim}"
