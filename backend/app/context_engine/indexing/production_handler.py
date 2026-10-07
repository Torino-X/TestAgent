"""生产 IndexJobHandler — 把 chunk 写入 Qdrant / ES 外部索引。

WP-BE-01：IndexWorker 的 channel job handler 生产实现（此前只有基类抛
NotImplementedError）。从 .env 构造 Embedding Provider + Qdrant / ES Store，
把解析好的 chunk 写入外部索引，并在 document 上回写 embedding 元数据。

- Embedding 不可用 → 记 document.vector_index_status='skipped'（不伪造 dense）；
- Qdrant / ES 缺库或不可达 → handler 抛出 RetryableIndexError → Worker 按
  retry/degraded 语义处理（与 CE-03 WP-4 降级一致）；
- 写入外部索引的 payload 恒带 owner（user_id + workspace_key）+ status='active'
  + deleted_at=None，保证检索端一级 ACL 正确。
"""

from __future__ import annotations

import logging
from typing import Any

from app.context_engine.indexing.index_worker import IndexJobHandler, RetryableIndexError

logger = logging.getLogger(__name__)


def _build_chunk_payload(*, document, chunk, workspace_key: str | None) -> dict[str, Any]:
    """构建写入外部索引的 payload（owner + ACL + 摘要）。"""
    payload = {
        "chunk_public_id": chunk.public_id,
        "document_public_id": document.public_id,
        "user_id": document.user_id,
        "workspace_key": workspace_key,
        "status": "active",
        "deleted_at": None,
        "content_excerpt": (chunk.content or "")[:400],
        "estimated_tokens": chunk.estimated_tokens,
        "source_public_id": document.source_public_id,
        "source_version": document.source_version,
    }
    metadata = getattr(document, "metadata_json", None) or {}
    for key in (
        "file_public_id",
        "document_kind",
        "semantic_labels",
        "profile_confidence",
        "profile_version",
    ):
        if key in metadata:
            payload[key] = metadata[key]
    return payload


class ProductionIndexJobHandler(IndexJobHandler):
    """生产 channel job 处理器：Embedding → Qdrant + ES。"""

    def __init__(
        self,
        *,
        vector_store=None,
        lexical_store=None,
        embedding_provider=None,
        embedding_model: str | None = None,
        embedding_dimension: int | None = None,
    ) -> None:
        self._vector_store = vector_store
        self._lexical_store = lexical_store
        self._embedding_provider = embedding_provider
        self._embedding_model = embedding_model
        self._embedding_dimension = embedding_dimension

    async def embed_document(
        self,
        *,
        session,
        document,
        chunks,
        embedding_namespace: str | None,
    ) -> None:
        """为每个 chunk 生成向量并 upsert 到 Qdrant（dense 通道）。

        Embedding Provider 或 Vector Store 不可用 → 抛 RetryableIndexError，
        Worker 按 retry/degraded 语义处理（不伪造 skipped）。
        """
        if self._vector_store is None:
            raise RetryableIndexError(
                "context.index.vector_store_unavailable",
                "向量存储未配置（无 Qdrant）",
            )
        if self._embedding_provider is None or not self._embedding_dimension:
            raise RetryableIndexError(
                "context.index.embedding_unavailable",
                "Embedding Provider 未配置",
            )
        if embedding_namespace is None:
            raise RetryableIndexError(
                "context.index.embedding_namespace_missing",
                "embedding namespace 未解析",
            )

        from app.context_engine.providers.protocols import (
            EmbeddingRequest,
            EmbeddingText,
        )

        texts = tuple(
            EmbeddingText(text_id=chunk.public_id, text=chunk.content or "")
            for chunk in chunks
            if (chunk.content or "").strip()
        )
        if not texts:
            raise RetryableIndexError(
                "context.index.empty_chunks",
                "没有可嵌入的 chunk 内容",
            )

        request = EmbeddingRequest(
            request_id=f"index_{document.public_id}",
            model=self._embedding_model or "",
            input_type="document",
            texts=texts,
            dimension=self._embedding_dimension,
            normalize=True,
        )
        result = await self._embedding_provider.embed(request)
        if not result.vectors:
            raise RetryableIndexError(
                "context.index.embedding_no_vectors",
                "Embedding 返回空向量",
            )

        vec_map = {v.text_id: v.values for v in result.vectors}
        # dimension mismatch 校验（与 RetrievalExecutor 语义一致）
        for v in result.vectors:
            if len(v.values) != self._embedding_dimension:
                raise RetryableIndexError(
                    "context.index.embedding_dimension_mismatch",
                    f"embedding dimension {len(v.values)} != {self._embedding_dimension}",
                )

        workspace_key = getattr(document, "workspace_key", None)
        for chunk in chunks:
            if (chunk.content or "").strip():
                vector = vec_map.get(chunk.public_id)
                if vector is None:
                    continue
                await self._vector_store.upsert_chunk(
                    namespace=embedding_namespace,
                    chunk_public_id=chunk.public_id,
                    vector=vector,
                    payload=_build_chunk_payload(
                        document=document, chunk=chunk, workspace_key=workspace_key
                    ),
                )

        # 回写 embedding 元数据（provider/model/dimension 审计）
        document.embedding_provider = self._embedding_model or "env_embedding"
        document.embedding_model = self._embedding_model
        document.embedding_dimension = self._embedding_dimension
        await session.flush()

    async def index_lexical(
        self,
        *,
        session,
        document,
        chunks,
        lexical_namespace: str | None,
    ) -> None:
        """把 chunk 写入 ES 词法索引（lexical 通道）。"""
        if self._lexical_store is None:
            raise RetryableIndexError(
                "context.index.lexical_store_unavailable",
                "词法存储未配置（无 Elasticsearch）",
            )
        if lexical_namespace is None:
            raise RetryableIndexError(
                "context.index.lexical_namespace_missing",
                "lexical namespace 未解析",
            )
        workspace_key = getattr(document, "workspace_key", None)
        for chunk in chunks:
            if not (chunk.content or "").strip():
                continue
            await self._lexical_store.index_chunk(
                namespace=lexical_namespace,
                chunk_public_id=chunk.public_id,
                normalized_content=chunk.normalized_content or "",
                payload=_build_chunk_payload(
                    document=document, chunk=chunk, workspace_key=workspace_key
                ),
            )
        await session.flush()

    async def delete_external(
        self,
        *,
        session,
        document,
        namespaces: list[str],
    ) -> None:
        """从外部索引删除文档的全部 chunk（幂等，缺库跳过）。"""
        from app.repositories.context_engine_repositories import (
            ContextIndexChunkRepository,
        )

        chunks = await ContextIndexChunkRepository(session).list_for_document(
            document.id, document.user_id
        )
        chunk_ids = [c.public_id for c in chunks]
        if not chunk_ids:
            return
        if self._vector_store is not None and namespaces and namespaces[0]:
            try:
                await self._vector_store.delete_chunks(
                    namespace=namespaces[0], chunk_public_ids=chunk_ids
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "index delete_external vector failed | doc=%s | err=%s",
                    document.public_id, type(exc).__name__,
                )
        if self._lexical_store is not None and len(namespaces) > 1 and namespaces[1]:
            try:
                await self._lexical_store.delete_chunks(
                    namespace=namespaces[1], chunk_public_ids=chunk_ids
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "index delete_external lexical failed | doc=%s | err=%s",
                    document.public_id, type(exc).__name__,
                )


__all__ = ["ProductionIndexJobHandler", "_build_chunk_payload"]
# auto-appended module-level note: 生产 indexer: 串起 upload → chunk → embed → store 全链路。
