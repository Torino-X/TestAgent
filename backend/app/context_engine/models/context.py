"""Context 领域模型：ContextItem / ContextScope / ContextRef / ContextRequest / ContextPlan。

遵循设计文档 §6.2 ContextItem schema、§8.2 Planner 输入输出。
Domain 模型不依赖 ORM / REST / LangGraph。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.context_engine.models.enums import (
    ContextKind,
    ContextTrust,
    RetrievalStrategy,
    RerankStrategy,
    SourceType,
)
from app.context_engine.models.value_objects import TokenCount, VersionString


class FrozenModel(BaseModel):
    """不可变 Domain 模型基类（禁止字段漂移）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ContextItem(FrozenModel):
    """统一候选对象（设计文档 §6.2）。"""

    item_id: str
    kind: ContextKind
    source_type: SourceType | str
    source_ref: str | None = None
    title: str | None = None
    content: str
    authority: int = Field(ge=0, le=100)
    relevance_score: float | None = Field(default=None, ge=0.0, le=1.0)
    rerank_score: float | None = Field(default=None, ge=0.0, le=1.0)
    priority: int = Field(default=0, ge=0)
    estimated_tokens: int = Field(default=0, ge=0)
    trust: ContextTrust = ContextTrust.UNTRUSTED_REFERENCE
    freshness_at: datetime | None = None
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextScope(FrozenModel):
    """作用域模型：所有来源、所有权与恢复引用都落在 Scope 上。

    ``workspace_key`` 允许为空（用户级作用域）。``thread_id`` 遵循
    ``thread_id = task_public_id`` 约束。
    """

    user_id: str
    workspace_key: str | None = None
    conversation_id: str | None = None
    task_id: str | None = None
    thread_id: str | None = None
    run_id: str | None = None

    @property
    def is_workspace_scoped(self) -> bool:
        return bool(self.workspace_key)

    def validate_ownership(self, owner_user_id: str, owner_workspace_key: str | None = None) -> bool:
        """所有权校验：用户必须匹配，workspace 若声明则必须匹配。"""
        if self.user_id != owner_user_id:
            return False
        if self.workspace_key and owner_workspace_key and self.workspace_key != owner_workspace_key:
            return False
        return True


class ContextRef(FrozenModel):
    """轻量可恢复引用（State-safe，不携带内容）。"""

    item_id: str
    kind: ContextKind
    source_type: SourceType | str
    source_ref: str | None = None
    version: VersionString | str | None = None

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind.value if isinstance(self.kind, ContextKind) else str(self.kind),
            "source_type": self.source_type.value if isinstance(self.source_type, SourceType) else str(self.source_type),
            "source_ref": self.source_ref,
            "version": self.version,
        }


class ContextRequest(FrozenModel):
    """Planner 输入（设计文档 §8.2.1）。"""

    user_id: str
    conversation_id: str | None = None
    conversation_public_id: str | None = None  # 公共 ID（如 conv_cd685e7f），供 scope 用
    task_id: str | None = None
    run_id: str | None = None
    agent_type: str | None = None
    call_site: str
    current_node: str | None = None
    current_user_message: str | None = None
    current_user_message_id: int | None = Field(default=None, gt=0)
    # Query seed used only for retrieval/ranking.  Idle context-usage previews
    # populate this from the latest persisted user focus without pretending
    # that the user has submitted another message.
    retrieval_query: str | None = None
    system_prompt: str | None = None
    output_contract: str | None = None
    intent_result: dict[str, Any] | None = None
    model_context_window: int | None = Field(default=None, gt=0)
    state_ref: dict[str, Any] | None = None
    attached_file_ids: list[str] = Field(default_factory=list)
    # Request-local staged image paths. They are never persisted into a
    # snapshot payload; the invoker forwards them only to a vision-capable
    # provider after the Context Engine has composed and audited the prompt.
    image_paths: list[str] = Field(default_factory=list)
    workspace_key: str | None = None
    thread_id: str | None = None
    # Read-only context-card baseline: a persisted current goal may be rendered
    # once while its message id is excluded from recent-history accounting.
    context_usage_baseline: bool = False
    # Canonical conversation working-set occupancy.  This is attached only to
    # active chat.reply calls and drives the 50% dialogue-retention waterline;
    # task profiles retain their own narrow projection budgets.
    conversation_ledger_tokens: int | None = Field(default=None, ge=0)

    @property
    def has_target(self) -> bool:
        return bool(self.call_site and self.user_id)


class ContextPlan(FrozenModel):
    """Planner 输出（设计文档 §8.2.2）。"""

    profile_key: str
    profile_version: VersionString | str
    model_context_window: int
    input_budget: TokenCount | int
    output_reserve: TokenCount | int
    runtime_reserve: TokenCount | int
    safety_margin: TokenCount | int
    soft_threshold: TokenCount | int = Field(default=0, ge=0)
    hard_compact_threshold: TokenCount | int = Field(default=0, ge=0)
    absolute_threshold: TokenCount | int = Field(default=0, ge=0)
    conversation_compact_threshold: TokenCount | int = Field(default=0, ge=0)
    section_plans: dict[str, "SectionPlan"] = Field(default_factory=dict)
    retrieval_queries: list[RetrievalQuery] = Field(default_factory=list)
    compression_policy: str | None = None
    fallback_chain: list[str] = Field(default_factory=list)


class SectionPlan(FrozenModel):
    """单个 Section 的预算计划。"""

    kind: ContextKind
    required: bool = False
    budget_tokens: TokenCount | int = Field(ge=0)
    source_types: list[SourceType | str] = Field(default_factory=list)
    allow_retrieval: bool = False


class RetrievalQuery(FrozenModel):
    """单条检索查询。"""

    query_text: str
    strategy: RetrievalStrategy = RetrievalStrategy.PLANNED
    source_family: str | None = None
    top_k: int = Field(default=5, ge=1, le=50)
    rerank_strategy: RerankStrategy = RerankStrategy.WEIGHTED_RRF


ContextPlan.model_rebuild()
# auto-appended module-level note: context 模型: ContextEngine 统一上下文快照(summary/recent turns/files/task state)。
