"""ContextSnapshot 生命周期模型：Ref / Complete / Fail / StateRef / Latency。

CE-02 WP-7：统一生命周期命令。``provider_request_id`` / ``sent_at`` 属
进程内状态（表无对应列，不创建新 Migration）；落库时 provider_request_id
归入 latency_json（设计文档 04 §51.3：latency_json → ContextBuildLatency）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.context_engine.models.context import ContextRef, FrozenModel
from app.context_engine.models.value_objects import Digest


class ContextSnapshotRef(FrozenModel):
    """轻量 Snapshot 引用（state-safe，可写入 State）。"""

    public_id: str
    status: str
    prompt_digest: Digest | None = None
    context_kind: str = "active"
    execution_mode: str = "active"

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "public_id": self.public_id,
            "status": self.status,
            "prompt_digest": str(self.prompt_digest) if self.prompt_digest else None,
            "context_kind": self.context_kind,
            "execution_mode": self.execution_mode,
        }


class ContextBuildLatency(FrozenModel):
    """构建各阶段耗时（落库到 latency_json，设计 04 §51.3）。"""

    planning_ms: int = Field(default=0, ge=0)
    source_gathering_ms: int = Field(default=0, ge=0)
    retrieval_ms: int = Field(default=0, ge=0)
    rerank_ms: int = Field(default=0, ge=0)
    selection_ms: int = Field(default=0, ge=0)
    preflight_ms: int = Field(default=0, ge=0)
    compose_ms: int = Field(default=0, ge=0)
    snapshot_ms: int = Field(default=0, ge=0)
    provider_ms: int = Field(default=0, ge=0)

    @property
    def total_ms(self) -> int:
        return (
            self.planning_ms
            + self.source_gathering_ms
            + self.retrieval_ms
            + self.rerank_ms
            + self.selection_ms
            + self.preflight_ms
            + self.compose_ms
            + self.snapshot_ms
            + self.provider_ms
        )


class ContextStateStats(FrozenModel):
    """ContextStateRef 内嵌统计（精简，不携带内容）。"""

    included_ref_count: int = Field(ge=0)
    dropped_ref_count: int = Field(ge=0)
    retrieval_run_count: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    degraded: bool = False
    locked_section_ids: list[str] = Field(default_factory=list)


class ContextStateRef(FrozenModel):
    """轻量 State-safe 引用（≤20KB，source_refs≤100，含 to_state_dict）。"""

    latest_snapshot_public_id: str
    profile_key: str
    stats: ContextStateStats
    source_refs: list[ContextRef] = Field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.source_refs) <= 100

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "latest_snapshot_public_id": self.latest_snapshot_public_id,
            "profile_key": self.profile_key,
            "stats": self.stats.model_dump(mode="json"),
            "source_refs": [r.to_state_dict() for r in self.source_refs],
        }


class ContextSnapshotCompleteCommand(FrozenModel):
    """complete 命令：actual tokens 只在真实 Provider usage 时写，否则 null。"""

    snapshot_public_id: str
    actual_input_tokens: int | None = Field(default=None, ge=0)
    actual_output_tokens: int | None = Field(default=None, ge=0)
    provider_request_id: str | None = Field(default=None, max_length=256)
    llm_latency_ms: int | None = Field(default=None, ge=0)
    completed_at: datetime | None = None  # None → writer 用当前时间


class ContextSnapshotFailCommand(FrozenModel):
    """fail 命令：携带安全 error_code（不含原始 Provider 错误全文）。"""

    snapshot_public_id: str
    error_code: str = Field(min_length=1, max_length=64)
    detail: str | None = Field(default=None, max_length=1000)
    failed_at: datetime | None = None  # None → writer 用当前时间


class ContextSnapshotBeginCommand(FrozenModel):
    """begin_build 命令：写完整 compose metadata（WP-7）。"""

    user_id: int = Field(gt=0)
    conversation_id: int | None = None
    agent_task_id: int | None = None
    llm_task_type: str = Field(min_length=1, max_length=64)
    context_kind: str = "active"
    call_site: str | None = Field(default=None, max_length=128)
    context_profile_key: str | None = None
    context_profile_version: str | None = None
    context_policy_version: str | None = None
    model_config_id: int | None = None
    model_name_snapshot: str | None = None
    context_window_tokens: int | None = None
    input_budget_tokens: int | None = None
    output_reserve_tokens: int | None = None
    runtime_reserve_tokens: int | None = None
    safety_margin_tokens: int | None = None
    target_input_tokens: int | None = None
    estimated_input_tokens: int = Field(default=0, ge=0)
    section_stats_json: dict[str, Any] | None = None
    included_refs_json: list[dict[str, Any]] | dict[str, Any] | None = None
    dropped_refs_json: list[dict[str, Any]] | dict[str, Any] | None = None
    prompt_digest: Digest | None = None
    prompt_excerpt: str | None = None
    latency_json: dict[str, Any] | None = None
    included_message_ids: list | None = None
    included_file_ids: list | None = None
    included_task_ids: list | None = None
    included_artifact_ids: list | None = None
    included_knowledge_ids: list | None = None
    summary_id: int | None = None
    retrieval_run_ids: list[str] = Field(default_factory=list)
    # Owner-safe preflight evidence.  This must contain only decision enums,
    # numeric token values, and public run references; no prompt/message text.
    preflight_json: dict[str, Any] | None = None
# auto-appended module-level note: snapshot 模型: LLMContextSnapshot(只存头部 + ids)。
