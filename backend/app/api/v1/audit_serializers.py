"""CE-05 Audit Serializers — 审计响应脱敏 DTO。

三类审计（Snapshot/Retrieval/Compaction）只读字段白名单；绝不返回
prompt/vector/secret/storage path/storage_key。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.core.logging_system.redaction import redact_text, redact_value


def _iso(dt) -> str | None:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:  # noqa: BLE001
        return str(dt)


def _truncate(value: str | None, limit: int = 200) -> str | None:
    if not value:
        return None
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _number(value: Any) -> float | None:
    """Convert database Decimal scores into JSON-safe scalars."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


_CANDIDATE_METADATA_ALLOWLIST = frozenset({
    "channel",
    "channels",
    "channel_ranks",
    "channel_scores",
})


def _safe_candidate_metadata(value: Any) -> dict[str, Any]:
    """Return only explainability metadata, then reuse central redaction."""
    if not isinstance(value, dict):
        return {}
    return {
        key: redact_value(value[key])
        for key in _CANDIDATE_METADATA_ALLOWLIST
        if key in value
    }


def serialize_snapshot(row: Any) -> dict:
    """Snapshot 审计只读 DTO。

    返回字段白名单：public_id/llm_task_type/context_kind/status/
    estimated_tokens/engine_version/call_site/context_policy_version/
    prompt_digest/created_at。绝不返回 context_preview/prompt_excerpt/
    full_prompt_payload_id/refs 正文。
    """
    return {
        "public_id": getattr(row, "public_id", None),
        "llm_task_type": getattr(row, "llm_task_type", None),
        "context_kind": getattr(row, "context_kind", None),
        "status": getattr(row, "status", None),
        "estimated_tokens": getattr(row, "estimated_tokens", 0),
        "engine_version": getattr(row, "engine_version", None),
        "call_site": getattr(row, "call_site", None),
        "context_policy_version": getattr(row, "context_policy_version", None),
        "prompt_digest": getattr(row, "prompt_digest", None),
        "budget": {
            "context_window_tokens": getattr(row, "context_window_tokens", None),
            "input_budget_tokens": getattr(row, "input_budget_tokens", None),
            "target_input_tokens": getattr(row, "target_input_tokens", None),
        },
        "token_usage": {
            "estimated_input_tokens": getattr(row, "estimated_input_tokens", None),
            "actual_input_tokens": getattr(row, "actual_input_tokens", None),
            "actual_output_tokens": getattr(row, "actual_output_tokens", None),
        },
        "preflight": _safe_preflight(getattr(row, "compaction_json", None)),
        "created_at": _iso(getattr(row, "created_at", None)),
    }


def _safe_preflight(value: Any) -> dict | None:
    """Return only the numeric/enum preflight telemetry stored on a snapshot."""
    if not isinstance(value, dict):
        return None
    keys = (
        "trigger",
        "waterline",
        "status",
        "action",
        "tokens_before",
        "tokens_after",
        "target_tokens",
        "conversation_ledger_tokens",
        "conversation_compact_threshold",
        "soft_threshold",
        "hard_threshold",
        "absolute_threshold",
        "dropped_ref_count",
        "compacted_summary_count",
        "compression_provider_call_count",
          "business_provider_call_count",
          "degraded",
          "blocked_reason",
          "compaction_attempted",
          "compaction_compactor_available",
          "compaction_runtime_context_available",
          "compaction_phase",
          "compaction_exception_type",
          "compaction_exception_code",
          "conversation_retention_eligible",
          "conversation_retention_rehydrated",
          "conversation_retention_candidate_count",
      )
    return {key: value.get(key) for key in keys if key in value}


def serialize_retrieval(row: Any) -> dict:
    """Retrieval 审计只读 DTO。

    只返回统计字段；query_excerpt 经截断（不返回 query 全文）。
    """
    return {
        "public_id": getattr(row, "public_id", None),
        "query_hash": getattr(row, "query_hash", None),
        "query_excerpt": _truncate(getattr(row, "query_excerpt", None), 200),
        "recalled_count": getattr(row, "recalled_count", 0),
        "selected_count": getattr(row, "selected_count", 0),
        "fallback_code": getattr(row, "fallback_code", None),
        # The persistence model names the field ``total_latency_ms``; keep
        # the established public API contract as ``total_ms``.
        "total_ms": getattr(row, "total_latency_ms", None),
        "created_at": _iso(getattr(row, "created_at", None)),
    }


def serialize_retrieval_candidate(
    row: Any,
    *,
    index_document_public_id: str | None,
    index_chunk_public_id: str | None,
) -> dict:
    """Candidate-level audit DTO with public IDs and redacted excerpts only."""
    excerpt = _truncate(redact_text(getattr(row, "content_excerpt", None) or ""), 512)
    return {
        "source_type": getattr(row, "source_type", None),
        "source_public_id": getattr(row, "source_public_id", None),
        "index_document_public_id": index_document_public_id,
        "index_chunk_public_id": index_chunk_public_id,
        "content_excerpt": excerpt,
        "estimated_tokens": getattr(row, "estimated_tokens", None),
        "raw_rank": getattr(row, "raw_rank", None),
        "raw_score": _number(getattr(row, "raw_score", None)),
        "normalized_score": _number(getattr(row, "normalized_score", None)),
        "rerank_score": _number(getattr(row, "rerank_score", None)),
        "final_rank": getattr(row, "final_rank", None),
        "selected": bool(getattr(row, "selected", False)),
        "drop_reason": getattr(row, "drop_reason", None),
        "source_quota_key": getattr(row, "source_quota_key", None),
        "metadata": _safe_candidate_metadata(getattr(row, "metadata_json", None)),
    }


def serialize_compaction(row: Any) -> dict:
    """Compaction 审计只读 DTO。

    refs 只回计数；recovery_payload_id 只回 public_id（不含 storage_key）；
    error_message 截断 ≤500。
    """
    return {
        "public_id": getattr(row, "public_id", None),
        "compaction_type": getattr(row, "compaction_type", None),
        "trigger_type": getattr(row, "trigger_type", None),
        "policy_key": getattr(row, "policy_key", None),
        "policy_version": getattr(row, "policy_version", None),
        "status": getattr(row, "status", None),
        "tokens_before": getattr(row, "tokens_before", None),
        "tokens_after": getattr(row, "tokens_after", None),
        "target_tokens": getattr(row, "target_tokens", None),
        "compression_ratio": getattr(row, "compression_ratio", None),
        "error_code": getattr(row, "error_code", None),
        "error_message": _truncate(getattr(row, "error_message", None), 500),
        "created_at": _iso(getattr(row, "created_at", None)),
    }


__all__ = [
    "serialize_snapshot",
    "serialize_retrieval",
    "serialize_retrieval_candidate",
    "serialize_compaction",
]


# 用途:三类审计(Snapshot/Retrieval/Compaction)响应 DTO 脱敏。
#
# 关键约束:
#   - 字段白名单:仅返回 public_id / task_public_id / created_at / kind /
#     status / token_count / metric 摘要等;
#   - 禁用:prompt / vector / secret / storage_path / storage_key /
#     storage_key_hash / 内部文件路径;
#   - owner-scope:owner 不在 payload 里体现,与 created_by_user_id 字段
#     隔离,审计响应只显示 owner 自己的;
#   - 跨用户访问走 404(从不泄露存在性)。
#
# 调用方:context_audit.py / context_observability.py / debug.py
