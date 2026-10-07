"""Compression 核心领域模型（FrozenModel）。

CE-04 WP-1：复用 models/context.py 的 FrozenModel 基类。核心 DTO 与
models/enums.py 的 CompactionType/CompactionTriggerType/RecoveryMode/
ContextPreflightAction 对齐。不创建同义核心模型。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.context_engine.models.context import ContextItem, ContextPlan, ContextRef, ContextRequest, FrozenModel
from app.context_engine.models.enums import (
    CompactionStatus,
    CompactionTriggerType,
    CompactionType,
    ContextPreflightAction,
    RecoveryMode,
)
from app.context_engine.models.profile import ContextBudget
from app.context_engine.models.selection import SelectedContextSet


class ProtectedAnchor(FrozenModel):
    """受保护锚点（state-safe，不携带原文）。"""

    key: str = Field(min_length=1, max_length=64)
    value_digest: str = Field(min_length=1, max_length=64)
    source_ref: str | None = None
    required_in_summary: bool = False
    kind: Literal["direct_inject", "summary_required"] = "summary_required"

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value_digest": self.value_digest,
            "source_ref": self.source_ref,
            "required_in_summary": self.required_in_summary,
            "kind": self.kind,
        }


class ContextPreflightRequest(FrozenModel):
    """Preflight 输入（doc09 §9）。"""

    request_id: str
    call_site: str
    profile_key: str
    budget: ContextBudget
    selected: SelectedContextSet
    protected_anchors: list[ProtectedAnchor] = Field(default_factory=list)
    trigger: CompactionTriggerType = CompactionTriggerType.PREFLIGHT
    token_counter: Any = None


class ContextPreflightResult(FrozenModel):
    """Preflight 输出（PASS / PRUNED / COMPACTED / BLOCKED）。"""

    status: CompactionStatus
    action: ContextPreflightAction
    selected: SelectedContextSet
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    target_tokens: int = Field(ge=0)
    compaction_runs: list[ContextRef] = Field(default_factory=list)
    compacted_summary_refs: list[ContextRef] = Field(default_factory=list)
    dropped_refs: list[ContextRef] = Field(default_factory=list)
    degraded: bool = False
    blocked_reason: str | None = None
    compression_provider_call_count: int = Field(default=0, ge=0)
    business_provider_call_count: int = Field(default=0, ge=0)
    # Content-free diagnostics for a failed automatic compaction attempt.
    # These may reach owner-scoped audit/response telemetry; never put exception
    # messages, prompt content, provider bodies, or credentials here.
    compaction_attempted: bool = False
    compaction_compactor_available: bool | None = None
    compaction_runtime_context_available: bool | None = None
    compaction_phase: str | None = None
    compaction_exception_type: str | None = None
    compaction_exception_code: str | None = None
    # Numeric/boolean-only retention diagnostics.  These identify why a chat
    # request did or did not enter the 60% conversation-retention branch
    # without exposing message text or source identities.
    conversation_retention_eligible: bool = False
    conversation_retention_rehydrated: bool = False
    conversation_retention_candidate_count: int = Field(default=0, ge=0)

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "action": str(self.action),
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "target_tokens": self.target_tokens,
            "degraded": self.degraded,
            "blocked_reason": self.blocked_reason,
            "compression_provider_call_count": self.compression_provider_call_count,
            "business_provider_call_count": self.business_provider_call_count,
            "compaction_attempted": self.compaction_attempted,
            "compaction_compactor_available": self.compaction_compactor_available,
            "compaction_runtime_context_available": self.compaction_runtime_context_available,
            "compaction_phase": self.compaction_phase,
            "compaction_exception_type": self.compaction_exception_type,
            "compaction_exception_code": self.compaction_exception_code,
            "conversation_retention_eligible": self.conversation_retention_eligible,
            "conversation_retention_rehydrated": self.conversation_retention_rehydrated,
            "conversation_retention_candidate_count": self.conversation_retention_candidate_count,
        }


class ContextCompactionRequest(FrozenModel):
    """Compaction 执行请求（Conversation / Agent Loop / Full Replace）。"""

    request_id: str
    user_id: int = Field(gt=0)
    conversation_id: int | None = None
    task_id: int | None = None
    workspace_key: str | None = None
    call_site: str
    compaction_type: CompactionType
    trigger: CompactionTriggerType
    policy_key: str
    policy_version: str
    source_digest: str = Field(min_length=1, max_length=64)
    tokens_before: int = Field(gt=0)
    target_tokens: int = Field(gt=0)
    protected_anchors: list[ProtectedAnchor] = Field(default_factory=list)
    source_refs: list[ContextRef] = Field(default_factory=list)
    recovery_mode: RecoveryMode = RecoveryMode.SUMMARY_ONLY
    require_recovery_payload: bool = False
    source_payload: dict[str, Any] = Field(default_factory=dict)
    # Conversation-summary coverage.  Manual compaction sets these fields so
    # later source collection can omit raw messages already represented by
    # the completed summary.  Other compaction types may leave them unset.
    covered_message_start_id: int | None = Field(default=None, gt=0)
    covered_message_end_id: int | None = Field(default=None, gt=0)
    covered_message_count: int | None = Field(default=None, ge=0)


class ContextCompactionResult(FrozenModel):
    """Compaction 执行结果。"""

    run_public_id: str
    summary_public_id: str | None = None
    summary_text: str | None = None
    summary_type: str | None = None
    replacement_items: list[ContextItem] = Field(default_factory=list)
    recovery_payload_id: int | None = None
    tokens_after: int = Field(ge=0)
    compression_ratio: float = Field(ge=0.0)
    status: CompactionStatus = CompactionStatus.COMPACTED
    error_code: str | None = None
    anchors_preserved: bool = True


class ContextRehydrateRequest(FrozenModel):
    """Rehydrate 请求。"""

    request_id: str
    user_id: int = Field(gt=0)
    workspace_key: str | None = None
    recovery_mode: RecoveryMode = RecoveryMode.SUMMARY_ONLY
    summary_public_id: str | None = None
    recovery_payload_id: int | None = None
    refs: list[ContextRef] = Field(default_factory=list)


class RehydratedContext(FrozenModel):
    """Rehydrate 输出（state-safe，恢复的 Ref 与可选段）。"""

    request_id: str
    recovery_mode: RecoveryMode
    summary_text: str | None = None
    summary_public_id: str | None = None
    recovered_refs: list[ContextRef] = Field(default_factory=list)
    evidence_segments: list[ContextRef] = Field(default_factory=list)
    rejected_refs: list[ContextRef] = Field(default_factory=list)
    degraded: bool = False


class EvidenceSegment(FrozenModel):
    """证据段：从被压缩内容提取的关键段落（写入 context_payloads）。"""

    segment_id: str
    source_ref: str | None = None
    digest: str = Field(min_length=1, max_length=64)
    kind: str = "evidence"
    content: str = Field(default="")


class RecoveryManifest(FrozenModel):
    """恢复清单：被压缩内容的 Ref 列表 + 版本 + digest（写入 context_payloads）。"""

    schema_version: str = "v1"
    policy_key: str | None = None
    source_refs: list[ContextRef] = Field(default_factory=list)
    source_versions: dict[str, str] = Field(default_factory=dict)
    source_digests: dict[str, str] = Field(default_factory=dict)
    segment_manifest: list[EvidenceSegment] = Field(default_factory=list)
    compression_policy: str | None = None
    created_at: datetime | None = None


__all__ = [
    "ProtectedAnchor",
    "ContextPreflightRequest",
    "ContextPreflightResult",
    "ContextCompactionRequest",
    "ContextCompactionResult",
    "ContextRehydrateRequest",
    "RehydratedContext",
    "EvidenceSegment",
    "RecoveryManifest",
]
# auto-appended module-level note: 压缩模型: CompressionContext / CompactionState。
