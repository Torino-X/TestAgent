"""Source Adapter 结果模型：SourceCollectResult / ContextWarning。

CE-02 WP-2：adapter 只做 fetch/ownership/normalize/token-estimate，
不直接消费结果；编排（Orchestrator）按 Section ID 聚合。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.context_engine.models.context import ContextItem, ContextKind, SourceType


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContextWarning(FrozenModel):
    """来源收集阶段的降级 / 警告信息（state-safe，不携带原始内容）。"""

    code: str = Field(min_length=1, max_length=64)
    detail: str = Field(min_length=1, max_length=500)
    adapter_key: str | None = None
    source_kind: str | None = None


class SourceCollectResult(FrozenModel):
    """单个 Source Adapter 的一次 collect 结果。"""

    adapter_key: str
    kind: ContextKind
    items: list[ContextItem] = Field(default_factory=list)
    warnings: list[ContextWarning] = Field(default_factory=list)
    locked_sections: list[LockedSection] = Field(default_factory=list)
    retrieval_run_ids: list[str] = Field(default_factory=list)
    attempted: bool = True
    degraded: bool = False
    failure_code: str | None = None
    latency_ms: int = Field(default=0, ge=0)

    @property
    def item_count(self) -> int:
        return len(self.items)

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "adapter_key": self.adapter_key,
            "kind": self.kind.value if isinstance(self.kind, ContextKind) else str(self.kind),
            "item_count": self.item_count,
            "warning_codes": [w.code for w in self.warnings],
            "retrieval_run_ids": self.retrieval_run_ids,
            "attempted": self.attempted,
            "degraded": self.degraded,
            "failure_code": self.failure_code,
            "latency_ms": self.latency_ms,
        }


class LockedSection(FrozenModel):
    """锁定章节元数据（WP-3b）。

    由 task_state / artifact Adapter 产出；selector / quota 识别
    ``locked=True`` 并豁免普通 quota；composer 渲染禁止修改声明。
    """

    section_id: str
    locked: bool = True
    authority: str = Field(min_length=1, max_length=128)
    version: str | None = None
    hash: str | None = None
    content_mode: str = "full_text"  # full_text | authoritative_summary | hash_ref
    item_id: str | None = None
    source_type: SourceType | str | None = None
# auto-appended module-level note: source 模型: SourceSpec / SourceDescriptor, sources 子包对外契约。
