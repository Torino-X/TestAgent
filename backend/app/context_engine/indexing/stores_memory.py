"""InMemory Vector / Lexical Store（离线测试 + 无外部服务降级）。

CE-03 WP-4：与 Qdrant / ES 共用同一 protocol 语义（ACL filter 一致）。
仅测试 / 降级使用，不用于生产数据（无跨 worker 持久化）。
"""

from __future__ import annotations

import math
from typing import Any

from app.context_engine.indexing.stores_protocol import (
    RetrievedChunk,
    LexicalStoreProtocol,
    VectorStoreProtocol,
    chunk_point_id,
)


def _owner_matches(user_id: int, workspace_key: str | None, payload: dict[str, Any]) -> bool:
    if payload.get("user_id") != user_id:
        return False
    if workspace_key is not None:
        if payload.get("workspace_key") != workspace_key:
            return False
    return True


def _active(payload: dict[str, Any]) -> bool:
    return payload.get("status") == "active" and payload.get("deleted_at") is None


def _cosine(a: list[float], b: list[float]) -> float:
    denom = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
    if denom == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / denom


class InMemoryVectorStore(VectorStoreProtocol):
    """进程内向量检索（dict：point_id → (vector, payload)）。"""

    def __init__(self) -> None:
        self._points: dict[str, tuple[list[float], dict[str, Any]]] = {}

    @property
    def enabled(self) -> bool:
        return True

    def ensure_namespace(self, namespace: str, dimension: int) -> None:
        return None

    async def upsert_chunk(
        self,
        *,
        namespace: str,
        chunk_public_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> None:
        key = f"{namespace}:{chunk_point_id(chunk_public_id)}"
        self._points[key] = (vector, dict(payload))

    async def delete_chunks(self, *, namespace: str, chunk_public_ids: list[str]) -> None:
        for cid in chunk_public_ids:
            self._points.pop(f"{namespace}:{chunk_point_id(cid)}", None)

    async def search(
        self,
        *,
        namespace: str,
        vector: list[float],
        user_id: int,
        workspace_key: str | None,
        top_k: int,
    ) -> list[RetrievedChunk]:
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for key, (vec, payload) in self._points.items():
            if not key.startswith(namespace + ":"):
                continue
            if not _owner_matches(user_id, workspace_key, payload):
                continue
            if not _active(payload):
                continue
            score = _cosine(vector, vec)
            scored.append((score, key, payload))
        scored.sort(key=lambda t: t[0], reverse=True)
        out: list[RetrievedChunk] = []
        for rank, (score, key, payload) in enumerate(scored[:top_k], start=1):
            out.append(
                RetrievedChunk(
                    chunk_public_id=str(payload.get("chunk_public_id", "")),
                    document_public_id=payload.get("document_public_id"),
                    user_id=int(payload.get("user_id", 0)),
                    workspace_key=payload.get("workspace_key"),
                    score=float(score),
                    channel="vector",
                    rank=rank,
                    content_excerpt=payload.get("content_excerpt"),
                    estimated_tokens=payload.get("estimated_tokens"),
                    source_public_id=payload.get("source_public_id"),
                    source_version=payload.get("source_version"),
                )
            )
        return out


class InMemoryLexicalStore(LexicalStoreProtocol):
    """进程内词法检索（term 命中 + 简单词频，deterministic）。"""

    def __init__(self) -> None:
        self._docs: dict[str, tuple[str, dict[str, Any]]] = {}

    @property
    def enabled(self) -> bool:
        return True

    def ensure_index(self, namespace: str) -> None:
        return None

    async def index_chunk(
        self,
        *,
        namespace: str,
        chunk_public_id: str,
        normalized_content: str,
        payload: dict[str, Any],
    ) -> None:
        key = f"{namespace}:{chunk_point_id(chunk_public_id)}"
        self._docs[key] = (normalized_content, dict(payload))

    async def delete_chunks(self, *, namespace: str, chunk_public_ids: list[str]) -> None:
        for cid in chunk_public_ids:
            self._docs.pop(f"{namespace}:{chunk_point_id(cid)}", None)

    async def search(
        self,
        *,
        namespace: str,
        query_text: str,
        user_id: int,
        workspace_key: str | None,
        top_k: int,
    ) -> list[RetrievedChunk]:
        terms = [t for t in query_text.lower().split() if len(t) > 1]
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for key, (content, payload) in self._docs.items():
            if not key.startswith(namespace + ":"):
                continue
            if not _owner_matches(user_id, workspace_key, payload):
                continue
            if not _active(payload):
                continue
            hits = sum(content.count(t) for t in terms)
            if hits == 0:
                continue
            score = float(hits)
            scored.append((score, key, payload))
        scored.sort(key=lambda t: t[0], reverse=True)
        out: list[RetrievedChunk] = []
        for rank, (score, key, payload) in enumerate(scored[:top_k], start=1):
            out.append(
                RetrievedChunk(
                    chunk_public_id=str(payload.get("chunk_public_id", "")),
                    document_public_id=payload.get("document_public_id"),
                    user_id=int(payload.get("user_id", 0)),
                    workspace_key=payload.get("workspace_key"),
                    score=float(score),
                    channel="lexical",
                    rank=rank,
                    content_excerpt=payload.get("content_excerpt"),
                    estimated_tokens=payload.get("estimated_tokens"),
                    source_public_id=payload.get("source_public_id"),
                    source_version=payload.get("source_version"),
                )
            )
        return out
# auto-appended module-level note: memory 存储: 内存版 chunk / embedding 索引(测试 + 开发)。
