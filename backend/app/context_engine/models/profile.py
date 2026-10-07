"""ContextProfile / ContextBudget 领域模型。

Profile 用代码版本管理（设计文档 §24.2 不创建 context_profiles 表）。
Budget 计算来自 ModelConfig 窗口 + 代码 Policy 阈值（§8.3）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.context_engine.models.enums import (
    CompressionLevel,
    ContextKind,
    PayloadMode,
    RerankStrategy,
    RetrievalStrategy,
)
from app.context_engine.models.value_objects import TokenCount, VersionString


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProfileSectionSpec(FrozenModel):
    """Profile 中单个 Section 的声明。"""

    kind: ContextKind
    required: bool = False
    max_budget_tokens: TokenCount | int | None = Field(default=None, ge=0)
    source_types: list[str] = Field(default_factory=list)
    allow_retrieval: bool = False


class BudgetPolicy(FrozenModel):
    """预算策略：阈值全部来自代码 Policy（§8.3.2），不硬编码单一 85%。"""

    target_input_ratio: float = Field(default=0.6, ge=0.1, le=0.95)
    # Optional proactive compaction line for conversational profiles.  It is
    # independent from Soft/Hard/Absolute: it is measured against the model
    # context window shown in the UI, while the other waterlines use the
    # reserve-adjusted usable input budget.
    conversation_compact_ratio: float | None = Field(default=None, ge=0.1, le=0.95)
    soft_ratio: float = Field(default=0.7, ge=0.1, le=0.95)
    hard_compact_ratio: float = Field(default=0.85, ge=0.1, le=0.98)
    absolute_ratio: float = Field(default=0.95, ge=0.1, le=1.0)
    output_reserve_tokens: TokenCount | int = Field(default=0, ge=0)
    runtime_tool_reserve_tokens: TokenCount | int = Field(default=0, ge=0)
    provider_overhead_tokens: TokenCount | int = Field(default=0, ge=0)
    safety_margin_ratio: float = Field(default=0.075, ge=0.0, le=0.5)

    @model_validator(mode="after")
    def _validate_monotonic(self) -> "BudgetPolicy":
        values = [
            self.target_input_ratio,
            self.soft_ratio,
            self.hard_compact_ratio,
            self.absolute_ratio,
        ]
        if any(b < a for a, b in zip(values, values[1:])):
            raise ValueError("预算阈值必须单调递增: target <= soft <= hard <= absolute")
        if (
            self.conversation_compact_ratio is not None
            and self.conversation_compact_ratio > self.hard_compact_ratio
        ):
            raise ValueError("conversation compact threshold must not exceed hard compact threshold")
        return self


class ContextProfile(FrozenModel):
    """某个 LLM 调用场景的输入合同（设计文档 §8.1）。

    与 LLMTaskProfile（输出合同）通过 ``context_profile_key`` 关联。
    """

    key: str = Field(min_length=1, max_length=128)
    version: VersionString | str = "v1"
    description: str | None = None
    required_sections: list[ProfileSectionSpec] = Field(default_factory=list)
    optional_sections: list[ProfileSectionSpec] = Field(default_factory=list)
    retrieval_policy: str | None = None
    budget_policy: BudgetPolicy = Field(default_factory=BudgetPolicy)
    compression_policy: str | None = None
    tool_output_policy: PayloadMode = PayloadMode.REFERENCE
    allow_long_term_memory: bool = False
    fallback_chain: list[str] = Field(default_factory=list)

    @property
    def all_sections(self) -> list[ProfileSectionSpec]:
        return list(self.required_sections) + list(self.optional_sections)

    def required_kind_set(self) -> set[ContextKind]:
        return {s.kind for s in self.required_sections}

    @model_validator(mode="after")
    def _required_sections_unique(self) -> "ContextProfile":
        kinds = [s.kind for s in self.required_sections]
        if len(kinds) != len(set(kinds)):
            raise ValueError("required_sections 中的 ContextKind 不允许重复")
        return self


class ContextBudget(FrozenModel):
    """单次调用的预算快照（§8.3.1 基本公式）。"""

    model_context_window: TokenCount | int
    output_reserve: TokenCount | int = Field(default=0, ge=0)
    runtime_reserve: TokenCount | int = Field(default=0, ge=0)
    provider_overhead: TokenCount | int = Field(default=0, ge=0)
    safety_margin: TokenCount | int = Field(default=0, ge=0)
    target_input: TokenCount | int = Field(default=0, ge=0)
    soft_threshold: TokenCount | int = Field(default=0, ge=0)
    hard_compact_threshold: TokenCount | int = Field(default=0, ge=0)
    absolute_threshold: TokenCount | int = Field(default=0, ge=0)
    conversation_compact_threshold: TokenCount | int = Field(default=0, ge=0)

    def level_for(self, estimated_tokens: int) -> CompressionLevel:
        """根据已估算 token 决定压缩水位。"""
        if self.absolute_threshold and estimated_tokens >= self.absolute_threshold:
            return CompressionLevel.ABSOLUTE
        if self.hard_compact_threshold and estimated_tokens >= self.hard_compact_threshold:
            return CompressionLevel.HARD_COMPACT
        if self.soft_threshold and estimated_tokens >= self.soft_threshold:
            return CompressionLevel.SOFT
        return CompressionLevel.TARGET

    @property
    def usable_window(self) -> int:
        return int(
            self.model_context_window
            - self.output_reserve
            - self.runtime_reserve
            - self.provider_overhead
            - self.safety_margin
        )

    @property
    def valid(self) -> bool:
        return self.usable_window > 0 and self.target_input <= self.usable_window
# auto-appended module-level note: profile 模型: 治理/缓存/压缩 纵向截面使用的 frozen dataclass。
