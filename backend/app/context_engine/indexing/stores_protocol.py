"""Vector / Lexical Store 协议与 Qdrant / Elasticsearch Adapter。

CE-03 WP-4：
- namespace identity 稳定 hash 生成合法 collection/index 名；
- point/_id = UUIDv5（chunk_public_id 非合法 UUID），chunk_public_id 放 payload；
- search 恒带 user_id + workspace + status='active' + deleted_at IS NULL 过滤；
- 缺库/无服务 → Disabled（不伪造）；InMemory 供离线测试/降级。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, Protocol


# ── namespace 稳定 hash ────────────────────────────────────────────────


def build_namespace_identity(
    *,
    provider_type: str,
    provider_config_public_id: str,
    model: str,
    dimension: int,
    normalize: bool,
    chunk_policy_key: str,
    index_schema_version: str = "v1",
) -> str:
    """namespace identity（第 7 项）。

    包含 provider type + config public_id + model + observed dimension +
    normalize + chunk_policy_key + index schema version。
    """
    return "|".join(
        [
            provider_type,
            provider_config_public_id,
            model,
            str(dimension),
            "norm" if normalize else "raw",
            chunk_policy_key,
            index_schema_version,
        ]
    )


def namespace_hash(namespace_identity: str) -> str:
    return hashlib.sha256(namespace_identity.encode("utf-8")).hexdigest()[:24]


def vector_namespace_name(namespace_identity: str) -> str:
    return f"ctx_vec_{namespace_hash(namespace_identity)}"


def lexical_namespace_name(namespace_identity: str) -> str:
    return f"ctx_lex_{namespace_hash(namespace_identity)}"


# ── UUIDv5 point id（chunk_public_id 非合法 UUID）──────────────────────


def chunk_point_id(chunk_public_id: str) -> str:
    """基于固定 namespace 的稳定 UUIDv5。"""
    ns = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # DNS namespace
    return str(uuid.uuid5(ns, chunk_public_id))


# ── 检索结果 DTO ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetrievedChunk:
    """外部召回候选（一级 ACL 后，仍待 MySQL 权威复核）。"""

    chunk_public_id: str
    document_public_id: str | None
    user_id: int
    workspace_key: str | None
    score: float
    channel: str  # lexical | vector
    rank: int
    content_excerpt: str | None = None
    estimated_tokens: int | None = None
    source_public_id: str | None = None
    source_version: str | None = None


# ── Vector Store Protocol ──────────────────────────────────────────────


class VectorStoreProtocol(Protocol):
    """Qdrant（或 InMemory）向量检索契约。"""

    def ensure_namespace(self, namespace: str, dimension: int) -> None: ...

    async def upsert_chunk(
        self,
        *,
        namespace: str,
        chunk_public_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> None: ...

    async def delete_chunks(self, *, namespace: str, chunk_public_ids: list[str]) -> None: ...

    async def search(
        self,
        *,
        namespace: str,
        vector: list[float],
        user_id: int,
        workspace_key: str | None,
        top_k: int,
    ) -> list[RetrievedChunk]: ...

    @property
    def enabled(self) -> bool: ...


# ── Lexical Store Protocol ─────────────────────────────────────────────


class LexicalStoreProtocol(Protocol):
    """Elasticsearch（或 InMemory）词法检索契约。"""

    def ensure_index(self, namespace: str) -> None: ...

    async def index_chunk(
        self,
        *,
        namespace: str,
        chunk_public_id: str,
        normalized_content: str,
        payload: dict[str, Any],
    ) -> None: ...

    async def delete_chunks(self, *, namespace: str, chunk_public_ids: list[str]) -> None: ...

    async def search(
        self,
        *,
        namespace: str,
        query_text: str,
        user_id: int,
        workspace_key: str | None,
        top_k: int,
    ) -> list[RetrievedChunk]: ...

    @property
    def enabled(self) -> bool: ...
# auto-appended module-level note: stores 协议: chunk store + embedding store 接口。
