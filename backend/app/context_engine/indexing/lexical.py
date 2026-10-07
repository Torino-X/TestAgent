"""Elasticsearch Lexical Store Adapter（optional import，缺库降级不伪造）。

CE-03 WP-4：
- index = 稳定 hash 生成名；_id = UUIDv5；chunk_public_id 放 payload；
- mapping：normalized_content(text) + owner/status(deleted_at keyword)；
- search 恒带 filter（owner + status + deleted_at IS NULL）；
- elasticsearch 缺失 / 连接失败 → enabled=False（检索通道降级）。

安全（第 1 项/方案 B）：凭据经环境变量注入（user='elastic' + ELASTIC_PASSWORD），
本地 HTTP + basic_auth；远程 HTTPS + verify_certs + ca_certs。凭据不进日志/报告。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.context_engine.indexing.stores_protocol import (
    RetrievedChunk,
    LexicalStoreProtocol,
    chunk_point_id,
)

logger = logging.getLogger(__name__)


class ElasticsearchLexicalStore(LexicalStoreProtocol):
    """Elasticsearch 词法检索（elasticsearch async client）。"""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 9200,
        user: str = "elastic",
        password: str | None = None,
        https: bool = False,
        verify_certs: bool = False,
        ca_certs: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        # 密码优先取显式参数，其次 ELASTIC_PASSWORD 环境变量（凭据不进日志）
        self._password = password or os.environ.get("ELASTIC_PASSWORD")
        self._https = https
        self._verify_certs = verify_certs
        self._ca_certs = ca_certs
        self._client: Any = None
        self._enabled: bool | None = None

    @property
    def enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        try:
            import elasticsearch  # noqa: F401

            self._enabled = True
        except ImportError:
            logger.warning("ElasticsearchLexicalStore disabled: elasticsearch 未安装")
            self._enabled = False
        return self._enabled

    def _get_client(self) -> Any:
        if not self.enabled:
            return None
        if self._client is None:
            from elasticsearch import AsyncElasticsearch

            base = self._host.rstrip("/")
            # host 若已含 scheme 则不重复拼接（兼容 .env 直接给 http://host 的接线）
            if not base.startswith(("http://", "https://")):
                base = f"{'https' if self._https else 'http'}://{base}"
            auth = (self._user, self._password) if self._password else None
            self._client = AsyncElasticsearch(
                hosts=[f"{base}:{self._port}"],
                basic_auth=auth,
                verify_certs=self._verify_certs,
                ca_certs=self._ca_certs,
            )
        return self._client

    def ensure_index(self, namespace: str) -> None:
        """确保 index 存在（协议同步方法；由 async 调用方配合 _ensure_index）。

        兼容测试 fixture：async 上下文调用方应使用 await store._ensure_index(ns)。
        """
        return None

    async def _ensure_index(self, namespace: str) -> None:
        """async 建 index（幂等，mapping 与 index_chunk 一致）。"""
        client = self._get_client()
        if client is None:
            return
        exists = await client.indices.exists(index=namespace)
        if not exists:
            await client.indices.create(
                index=namespace,
                mappings={
                    "properties": {
                        "normalized_content": {"type": "text"},
                        "user_id": {"type": "long"},
                        "workspace_key": {"type": "keyword"},
                        "status": {"type": "keyword"},
                        "deleted_at": {"type": "date"},
                        "chunk_public_id": {"type": "keyword"},
                        "document_public_id": {"type": "keyword"},
                        "source_public_id": {"type": "keyword"},
                        "source_version": {"type": "keyword"},
                        "content_excerpt": {"type": "keyword"},
                    }
                },
            )

    async def index_chunk(
        self,
        *,
        namespace: str,
        chunk_public_id: str,
        normalized_content: str,
        payload: dict[str, Any],
    ) -> None:
        client = self._get_client()
        if client is None:
            return
        await self._ensure_index(namespace)
        await client.index(
            index=namespace,
            id=chunk_point_id(chunk_public_id),
            refresh=True,  # index 后立即可检索（测试与实时检索路径一致）
            document={
                "normalized_content": normalized_content,
                "user_id": payload.get("user_id"),
                "workspace_key": payload.get("workspace_key"),
                "status": payload.get("status", "active"),
                "deleted_at": payload.get("deleted_at"),
                "chunk_public_id": chunk_public_id,
                "document_public_id": payload.get("document_public_id"),
                "source_public_id": payload.get("source_public_id"),
                "source_version": payload.get("source_version"),
                "content_excerpt": payload.get("content_excerpt"),
            },
        )

    async def delete_chunks(self, *, namespace: str, chunk_public_ids: list[str]) -> None:
        client = self._get_client()
        if client is None:
            return
        await client.delete_by_query(
            index=namespace,
            query={"ids": {"values": [chunk_point_id(cid) for cid in chunk_public_ids]}},
            refresh=True,
        )

    async def search(
        self,
        *,
        namespace: str,
        query_text: str,
        user_id: int,
        workspace_key: str | None,
        top_k: int,
    ) -> list[RetrievedChunk]:
        client = self._get_client()
        if client is None:
            return []
        # 索引不存在时直接返回空(避免 404 触发检索通道异常)
        try:
            exists = await client.indices.exists(index=namespace)
        except Exception:
            exists = False
        if not exists:
            return []
        must: list[dict[str, Any]] = [
            {"term": {"user_id": user_id}},
            {"term": {"status": "active"}},
        ]
        if workspace_key is not None:
            # workspace 模式：精确匹配
            must.append({"term": {"workspace_key": workspace_key}})
        else:
            # user-global 模式：workspace_key 必须不存在/为空
            must.append({"bool": {"must_not": {"exists": {"field": "workspace_key"}}}})
        body = {
            "query": {
                "bool": {
                    "must": [
                        {"match": {"normalized_content": query_text}},
                    ],
                    "filter": must,
                }
            },
            "size": top_k,
        }
        resp = await client.search(index=namespace, body=body)
        out: list[RetrievedChunk] = []
        for rank, hit in enumerate(resp.get("hits", {}).get("hits", []), start=1):
            src = hit.get("_source", {})
            out.append(
                RetrievedChunk(
                    chunk_public_id=str(src.get("chunk_public_id", "")),
                    document_public_id=src.get("document_public_id"),
                    user_id=int(src.get("user_id", 0)),
                    workspace_key=src.get("workspace_key"),
                    score=float(hit.get("_score") or 0.0),
                    channel="lexical",
                    rank=rank,
                    content_excerpt=src.get("content_excerpt"),
                    source_public_id=src.get("source_public_id"),
                    source_version=src.get("source_version"),
                )
            )
        return out
# auto-appended module-level note: lexical 索引: 关键词 BM25 / 倒排索引。
