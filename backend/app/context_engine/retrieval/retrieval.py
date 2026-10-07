"""检索管线：QueryBuilder / RetrievalExecutor / RetrievalAuditService。

CE-03 WP-5：
- QueryBuilder：由 Plan/Profile/SectionPlan 生成 typed RetrievalRequest
  （ON_DEMAND 由确定性规则显式生成，不依赖 current_user_message 重建）；
- RetrievalExecutor（Knowledge 用）：ES lexical + Qdrant dense 双通道召回 →
  一级外部 filter → MySQL 权威复核（二级 ACL）→ dedup → Weighted RRF /
  RerankService → top-k → 写 context_retrieval_runs / candidates；
- RetrievalAuditService：Knowledge/Memory 统一审计写入口
  （Memory 只写统计字段，正文/Evidence 不进审计表）。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.context_engine.models.context import ContextPlan, SectionPlan
from app.context_engine.models.enums import ContextKind, RerankStrategy, RetrievalStrategy
from app.context_engine.models.retrieval import RetrievalRequest, RetrievalScopeFilter
from app.context_engine.models.source import ContextWarning, SourceCollectResult

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _gen_public_id(prefix: str) -> str:
    import uuid

    return prefix + uuid.uuid4().hex[:40]


def query_hash(query_text: str) -> str:
    return hashlib.sha256(query_text.encode("utf-8")).hexdigest()


# ── QueryBuilder ───────────────────────────────────────────────────────


class QueryBuilder:
    """由 Plan / Profile / SectionPlan 生成 typed RetrievalRequest。

    确定性规则：
    - Plan.retrieval_queries（PLANNED）逐条转换为 RetrievalRequest；
    - 若 section allow_retrieval 且无对应 planned query → 生成 ON_DEMAND。
    """

    def __init__(self, *, default_top_k: int = 5) -> None:
        self._default_top_k = default_top_k

    def build(
        self,
        *,
        user_internal_id: int,
        workspace_key: str | None,
        agent_type: str | None,
        plan: ContextPlan | None,
        section_plans: list[SectionPlan],
    ) -> list[RetrievalRequest]:
        requests: list[RetrievalRequest] = []
        seen_families: set[str] = set()

        if plan and plan.retrieval_queries:
            for q in plan.retrieval_queries:
                scope = RetrievalScopeFilter(
                    mode="union",
                    user_id=user_internal_id,
                    workspace_key=workspace_key,
                    agent_type=agent_type,
                )
                requests.append(
                    RetrievalRequest(
                        query_text=q.query_text,
                        strategy=q.strategy,
                        source_family=q.source_family,
                        top_k=q.top_k,
                        rerank_strategy=q.rerank_strategy,
                        scope=scope,
                    )
                )
                if q.source_family:
                    seen_families.add(q.source_family)

        for section in section_plans:
            if not section.allow_retrieval:
                continue
            family = _family_for_kind(section.kind)
            if family is None or family in seen_families:
                continue
            scope = RetrievalScopeFilter(
                mode="union",
                user_id=user_internal_id,
                workspace_key=workspace_key,
                agent_type=agent_type,
            )
            requests.append(
                RetrievalRequest(
                    query_text="",
                    strategy=RetrievalStrategy.ON_DEMAND,
                    source_family=family,
                    top_k=self._default_top_k,
                    rerank_strategy=RerankStrategy.WEIGHTED_RRF,
                    scope=scope,
                )
            )
            seen_families.add(family)

        return requests


def _family_for_kind(kind: ContextKind) -> str | None:
    if kind == ContextKind.KNOWLEDGE:
        return "knowledge"
    if kind == ContextKind.MEMORY:
        return "memory"
    if kind == ContextKind.EVIDENCE:
        return "artifact"
    return None


# ── Weighted RRF ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class FusionResult:
    chunk_public_id: str
    document_public_id: str | None
    user_id: int
    workspace_key: str | None
    final_rank: int
    raw_score: float
    normalized_score: float
    rerank_score: float | None
    channel: str
    content_excerpt: str | None = None
    source_public_id: str | None = None
    source_version: str | None = None


def weighted_rrf(
    lists: list[list[FusionResult]],
    *,
    k: int = 60,
    weights: list[float] | None = None,
) -> list[FusionResult]:
    """确定性 Weighted RRF：score = Σ w_i / (k + rank_i)。"""
    n = len(lists)
    weights = weights or [1.0 / n] * n
    acc: dict[str, dict[str, Any]] = {}

    for lst, w in zip(lists, weights):
        for rank, item in enumerate(lst, start=1):
            entry = acc.setdefault(
                item.chunk_public_id,
                {
                    "chunk_public_id": item.chunk_public_id,
                    "document_public_id": item.document_public_id,
                    "user_id": item.user_id,
                    "workspace_key": item.workspace_key,
                    "channel": item.channel,
                    "content_excerpt": item.content_excerpt,
                    "source_public_id": item.source_public_id,
                    "source_version": item.source_version,
                },
            )
            entry["rrf"] = entry.get("rrf", 0.0) + w / (k + rank)

    merged = []
    for cid, entry in acc.items():
        merged.append(
            FusionResult(
                chunk_public_id=entry["chunk_public_id"],
                document_public_id=entry["document_public_id"],
                user_id=entry["user_id"],
                workspace_key=entry["workspace_key"],
                final_rank=0,
                raw_score=0.0,
                normalized_score=float(entry["rrf"]),
                rerank_score=None,
                channel=entry["channel"],
                content_excerpt=entry["content_excerpt"],
                source_public_id=entry["source_public_id"],
                source_version=entry["source_version"],
            )
        )
    merged.sort(key=lambda m: m.normalized_score, reverse=True)
    for i, m in enumerate(merged, start=1):
        m = FusionResult(
            chunk_public_id=m.chunk_public_id,
            document_public_id=m.document_public_id,
            user_id=m.user_id,
            workspace_key=m.workspace_key,
            final_rank=i,
            raw_score=m.normalized_score,
            normalized_score=m.normalized_score,
            rerank_score=m.rerank_score,
            channel=m.channel,
            content_excerpt=m.content_excerpt,
            source_public_id=m.source_public_id,
            source_version=m.source_version,
        )
        merged[i - 1] = m
    return merged


# ── MySQL 权威复核（第二级 ACL）───────────────────────────────────────


@dataclass
class RecheckResult:
    approved: list[dict[str, Any]] = field(default_factory=list)
    dropped_reasons: list[str] = field(default_factory=list)
    rejected_by_chunk: dict[str, str] = field(default_factory=dict)
    observed_by_chunk: dict[str, dict[str, Any]] = field(default_factory=dict)


async def mysql_authoritative_recheck(
    *,
    session,
    chunk_public_ids: list[str],
    user_id: int,
    workspace_key: str | None,
    lexical_channel: bool,
    vector_channel: bool,
    channels_by_chunk: dict[str, set[str]] | None = None,
    allowed_source_types: list[str] | None = None,
    allowed_source_public_ids: list[str] | None = None,
    allowed_document_public_ids: list[str] | None = None,
    allowed_source_versions: dict[str, str] | None = None,
) -> RecheckResult:
    """用 document_id/chunk_id 回 MySQL 批量复核 owner/status/version。

    通过条件：document.status='indexed' + 对应 channel status='ready' +
    chunk status='active' + deleted_at null + owner 匹配。
    未通过 → drop_reason ∈ {acl_filtered, deleted, stale_version}。
    """
    from sqlalchemy import select
    from app.models.context_engine import ContextIndexChunk, ContextIndexDocument

    if not chunk_public_ids:
        return RecheckResult()

    result = RecheckResult()
    stmt = (
        select(ContextIndexChunk, ContextIndexDocument)
        .join(ContextIndexDocument, ContextIndexDocument.id == ContextIndexChunk.document_id)
        .where(ContextIndexChunk.public_id.in_(chunk_public_ids))
    )
    rows = (await session.execute(stmt)).all()

    by_chunk = {chunk.public_id: (chunk, doc) for chunk, doc in rows}

    for cid in chunk_public_ids:
        pair = by_chunk.get(cid)
        if pair is None:
            result.dropped_reasons.append("deleted")
            result.rejected_by_chunk[cid] = "deleted"
            continue
        chunk, doc = pair
        observed = {
            "chunk_public_id": chunk.public_id,
            "chunk_id": chunk.id,
            "document_public_id": doc.public_id,
            "document_id": doc.id,
            "user_id": chunk.user_id,
            "workspace_key": doc.workspace_key,
            "content": chunk.content,
            "content_hash": chunk.content_hash,
            "estimated_tokens": chunk.estimated_tokens,
            "source_public_id": doc.source_public_id,
            "source_version": doc.source_version,
        }
        # This is audit enrichment only.  It preserves public-ID mapping and a
        # bounded excerpt even when the authoritative checks reject the item.
        result.observed_by_chunk[cid] = observed
        if chunk.user_id != user_id:
            result.dropped_reasons.append("acl_filtered")
            result.rejected_by_chunk[cid] = "acl_filtered"
            continue
        if doc.workspace_key and workspace_key and doc.workspace_key != workspace_key:
            result.dropped_reasons.append("acl_filtered")
            result.rejected_by_chunk[cid] = "acl_filtered"
            continue
        if doc.status != "indexed" or chunk.status != "active":
            result.dropped_reasons.append("deleted")
            result.rejected_by_chunk[cid] = "deleted"
            continue
        if doc.deleted_at is not None or chunk.deleted_at is not None:
            result.dropped_reasons.append("deleted")
            result.rejected_by_chunk[cid] = "deleted"
            continue
        if allowed_source_types and doc.source_type not in set(allowed_source_types):
            result.dropped_reasons.append("source_filtered")
            result.rejected_by_chunk[cid] = "source_filtered"
            continue
        if allowed_source_public_ids and doc.source_public_id not in set(allowed_source_public_ids):
            result.dropped_reasons.append("source_filtered")
            result.rejected_by_chunk[cid] = "source_filtered"
            continue
        if allowed_document_public_ids and doc.public_id not in set(allowed_document_public_ids):
            result.dropped_reasons.append("source_filtered")
            result.rejected_by_chunk[cid] = "source_filtered"
            continue
        if allowed_source_versions and doc.source_version != allowed_source_versions.get(doc.source_public_id):
            result.dropped_reasons.append("stale_version")
            result.rejected_by_chunk[cid] = "stale_version"
            continue
        candidate_channels = (
            channels_by_chunk.get(cid, set())
            if channels_by_chunk is not None
            else set()
        )
        needs_lexical = (
            "lexical" in candidate_channels
            if channels_by_chunk is not None
            else lexical_channel
        )
        needs_vector = (
            "vector" in candidate_channels
            if channels_by_chunk is not None
            else vector_channel
        )
        if needs_lexical and doc.lexical_index_status != "ready":
            result.dropped_reasons.append("stale_version")
            result.rejected_by_chunk[cid] = "stale_version"
            continue
        if needs_vector and doc.vector_index_status != "ready":
            result.dropped_reasons.append("stale_version")
            result.rejected_by_chunk[cid] = "stale_version"
            continue
        result.approved.append(observed)
    return result
# auto-appended module-level note: retrieval 核心: 走 Knowledge Base + Memory + In-flight state 多源。
