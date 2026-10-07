"""Composer 结果模型：ContextMessage / ContextComposeResult / ComposeValidation。

CE-02 WP-4：固定 12 项消息顺序 + 三锚点 + Trust 包装。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.context_engine.models.context import ContextKind, ContextTrust
from app.context_engine.models.selection import SelectedContextSet
from app.context_engine.models.value_objects import Digest


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContextMessage(FrozenModel):
    """单条已注入消息。``role`` 仅允许 user/system（untrusted 永不能成为 system）。"""

    role: Literal["user", "system"]
    content: str
    kind: ContextKind | None = None
    trust: ContextTrust = ContextTrust.UNTRUSTED_REFERENCE
    source_ref: str | None = None
    section_id: str | None = None
    locked: bool = False
    authority: str | None = None


class ComposeValidation(FrozenModel):
    """Compose 输出验证结果（WP-4 validator）。"""

    ok: bool = True
    estimated_input_tokens: int = Field(default=0, ge=0)
    required_sections_present: bool = True
    current_goal_anchor_present: bool = True
    roles_valid: bool = True
    tool_call_ids_valid: bool = True
    injection_labels_present: bool = True
    digest: Digest | None = None
    prompt_excerpt: str | None = None
    failure_code: str | None = None


class ContextComposeResult(FrozenModel):
    """Compose 全量结果（含验证、快照引用、token 统计）。"""

    messages: list[ContextMessage] = Field(default_factory=list)
    selected: SelectedContextSet | None = None
    validation: ComposeValidation = Field(default_factory=ComposeValidation)
    prompt_text: str | None = None
    prompt_digest: Digest | None = None
    prompt_excerpt: str | None = None
    estimated_input_tokens: int = Field(default=0, ge=0)
    build_latency_ms: int = Field(default=0, ge=0)
    retrieval_run_ids: list[str] = Field(default_factory=list)
    degraded: bool = False
    snapshot_public_id: str | None = None
    context_kind: str = "active"
    execution_mode: str = "active"
    section_stats: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Preview calls are intentionally non-persistent. Keep only safe,
    # content-free preflight metadata so callers can distinguish a final
    # next-request estimate from a state that still needs real compaction.
    preflight: dict[str, Any] | None = None

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "message_count": len(self.messages),
            "roles": [m.role for m in self.messages],
            "validation": self.validation.model_dump(mode="json"),
            "prompt_digest": str(self.prompt_digest) if self.prompt_digest else None,
            "estimated_input_tokens": self.estimated_input_tokens,
            "build_latency_ms": self.build_latency_ms,
            "retrieval_run_count": len(self.retrieval_run_ids),
            "degraded": self.degraded,
            "snapshot_public_id": self.snapshot_public_id,
            "execution_mode": self.execution_mode,
        }
# auto-appended module-level note: compose 模型: assemble / compose 阶段的产出结构。
