"""CE-03 Indexing：文档索引、Chunking、Index Worker。

- chunker.py：RecursiveCharChunker（chunk_policy_key="recursive_char:v1"）
- document_service.py：uploaded_files → context_index_documents → enqueue
- index_worker.py：asyncio poll 领取 context_index_jobs 并处理
- vector_store.py / lexical.py：Qdrant / Elasticsearch Adapter（WP-4）
"""

from app.context_engine.indexing.chunker import (
    CHUNK_POLICY_KEY,
    Chunk,
    RecursiveCharChunker,
    compute_sha256,
    normalize_text,
)
from app.context_engine.indexing.document_service import (
    CHUNK_OPERATION,
    DELETE_EXTERNAL_OPERATION,
    EMBED_OPERATION,
    LEXICAL_OPERATION,
    DocumentIndexError,
    IndexDocumentService,
    compute_job_idempotency_key,
    compute_source_digest,
)
from app.context_engine.indexing.stores_protocol import (
    LexicalStoreProtocol,
    RetrievedChunk,
    VectorStoreProtocol,
    build_namespace_identity,
    chunk_point_id,
    lexical_namespace_name,
    namespace_hash,
    vector_namespace_name,
)
from app.context_engine.indexing.stores_memory import InMemoryLexicalStore, InMemoryVectorStore
from app.context_engine.indexing.vector_store import QdrantVectorStore
from app.context_engine.indexing.lexical import ElasticsearchLexicalStore

__all__ = [
    "CHUNK_POLICY_KEY",
    "Chunk",
    "RecursiveCharChunker",
    "compute_sha256",
    "normalize_text",
    "CHUNK_OPERATION",
    "DELETE_EXTERNAL_OPERATION",
    "EMBED_OPERATION",
    "LEXICAL_OPERATION",
    "DocumentIndexError",
    "IndexDocumentService",
    "compute_job_idempotency_key",
    "compute_source_digest",
    "LexicalStoreProtocol",
    "RetrievedChunk",
    "VectorStoreProtocol",
    "build_namespace_identity",
    "chunk_point_id",
    "lexical_namespace_name",
    "namespace_hash",
    "vector_namespace_name",
    "InMemoryLexicalStore",
    "InMemoryVectorStore",
    "QdrantVectorStore",
    "ElasticsearchLexicalStore",
]
# auto-appended module-level note: indexing 子包: 文档索引入口(chunker / vector / lexical / index_worker)。
