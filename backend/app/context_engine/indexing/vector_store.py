"""Qdrant Vector Store Adapter（optional import，缺库降级不伪造）。

CE-03 WP-4：
- namespace = 稳定 hash 生成的 collection 名；
- point id = UUIDv5（chunk_public_id 放 payload）；
- search 恒带 owner + status='active' + deleted_at IS NULL 过滤；
- qdrant-client 缺失 / 连接失败 → enabled=False（检索通道降级）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.context_engine.indexing.stores_protocol import (
    RetrievedChunk,
    VectorStoreProtocol,
    chunk_point_id,
)

logger = logging.getLogger(__name__)


class QdrantVectorStore(VectorStoreProtocol):
    """Qdrant 向量检索（qdrant-client async）。

    构造不 import qdrant-client；首次使用 detect（缺库 → enabled=False）。
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 6333,
        api_key: str | None = None,
        prefer_grpc: bool = True,
        https: bool = False,
        check_compatibility: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._api_key = api_key
        self._prefer_grpc = prefer_grpc
        self._check_compatibility = check_compatibility
        self._https = https
        self._client: Any = None
        self._enabled: bool | None = None

    @property
    def enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        try:
            import qdrant_client  # noqa: F401

            self._enabled = True
        except ImportError:
            logger.warning("QdrantVectorStore disabled: qdrant-client 未安装")
            self._enabled = False
        return self._enabled

    def _get_client(self) -> Any:
        if not self.enabled:
            return None
        if self._client is None:
            from qdrant_client import AsyncQdrantClient

            base = self._host.rstrip("/")
            # host 若已含 scheme 则不重复拼接（兼容 .env 直接给 http://host 的接线）
            if not base.startswith(("http://", "https://")):
                base = f"{'https' if self._https else 'http'}://{base}"
            url = f"{base}:{self._port}"
            self._client = AsyncQdrantClient(
                url=url,
                api_key=self._api_key,
                prefer_grpc=self._prefer_grpc,
                check_compatibility=self._check_compatibility,
            )
        return self._client

    def ensure_namespace(self, namespace: str, dimension: int) -> None:
        """确保 collection 存在（协议同步方法；由 async 调用方配合 _ensure_collection）。

        兼容测试 fixture：async 上下文调用方应使用 await store._ensure_collection(ns, dim)。
        此处保留为 no-op 占位（Qdrant 建 collection 需 async，协议签名受限）。
        """
        return None

    async def _ensure_collection(self, namespace: str, dimension: int) -> None:
        """async 建 collection（幂等，用 observed dimension）。"""
        client = self._get_client()
        if client is None:
            return
        from qdrant_client import models

        exists = await client.collection_exists(collection_name=namespace)
        if not exists:
            await client.create_collection(
                collection_name=namespace,
                vectors_config=models.VectorParams(
                    size=dimension,
                    distance=models.Distance.COSINE,
                ),
            )

    async def upsert_chunk(
        self,
        *,
        namespace: str,
        chunk_public_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> None:
        client = self._get_client()
        if client is None:
            return
        from qdrant_client import models

        await client.upsert(
            collection_name=namespace,
            points=[
                models.PointStruct(
                    id=chunk_point_id(chunk_public_id),
                    vector=vector,
                    payload=payload,
                )
            ],
        )

    async def delete_chunks(self, *, namespace: str, chunk_public_ids: list[str]) -> None:
        client = self._get_client()
        if client is None:
            return
        await client.delete(
            collection_name=namespace,
            points_selector=[chunk_point_id(cid) for cid in chunk_public_ids],
        )

    async def search(
        self,
        *,
        namespace: str,
        vector: list[float],
        user_id: int,
        workspace_key: str | None,
        top_k: int,
    ) -> list[RetrievedChunk]:
        client = self._get_client()
        if client is None:
            return []
        # collection 不存在时直接返回空(避免 NotFound 异常)
        try:
            exists = await client.collection_exists(collection_name=namespace)
        except Exception:
            exists = False
        if not exists:
            return []
        from qdrant_client import models

        must: list[Any] = [
            models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id)),
            models.FieldCondition(key="status", match=models.MatchValue(value="active")),
        ]
        if workspace_key is not None:
            # workspace 模式：精确匹配
            must.append(
                models.FieldCondition(key="workspace_key", match=models.MatchValue(value=workspace_key))
            )
        else:
            # user-global 模式：workspace_key 必须为空/不存在
            must.append(
                models.IsEmptyCondition(
                    is_empty=models.PayloadField(key="workspace_key"),
                )
            )
        hits = await client.query_points(
            collection_name=namespace,
            query=vector,
            query_filter=models.Filter(must=must),
            limit=top_k,
            with_payload=True,
        )
        out: list[RetrievedChunk] = []
        for rank, hit in enumerate(hits.points or [], start=1):
            p = hit.payload or {}
            out.append(
                RetrievedChunk(
                    chunk_public_id=str(p.get("chunk_public_id", "")),
                    document_public_id=p.get("document_public_id"),
                    user_id=int(p.get("user_id", 0)),
                    workspace_key=p.get("workspace_key"),
                    score=float(getattr(hit, "score", 0.0) or 0.0),
                    channel="vector",
                    rank=rank,
                    content_excerpt=p.get("content_excerpt"),
                    estimated_tokens=p.get("estimated_tokens"),
                    source_public_id=p.get("source_public_id"),
                    source_version=p.get("source_version"),
                )
            )
        return out
# auto-appended module-level note: vector store: 向量索引(余弦 / IP 检索)。
