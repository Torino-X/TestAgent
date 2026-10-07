"""Retrieval 领域模型：RetrievalRequest / RetrievalScopeFilter。

CE-03 WP-2：检索查询类型化 + 统一作用域过滤。
MySQL / ES / Qdrant 共用同一 ``RetrievalScopeFilter`` 语义与测试向量；
Adapter 只消费显式传入的 typed ``RetrievalRequest``，不自行规划。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.context_engine.models.enums import RerankStrategy, RetrievalStrategy


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


ScopeMode = Literal["user_global", "workspace", "union", "playbook"]


class RetrievalScopeFilter(FrozenModel):
    """统一检索作用域过滤（user-global / workspace / union / playbook）。

    - ``user_global``：user_id 相同 + workspace_key IS NULL
    - ``workspace``：user_id 相同 + workspace_key 精确匹配
    - ``union``：user_global UNION current workspace
    - ``playbook``：user_id 相同 + scope_type='agent_playbook'
      + agent_type 精确匹配 + workspace_key IS NULL
    """

    mode: ScopeMode
    user_id: int = Field(gt=0)
    workspace_key: str | None = None
    agent_type: str | None = None

    def matches_row(
        self,
        *,
        owner_user_id: int,
        owner_workspace_key: str | None = None,
        row_scope_type: str | None = None,
        row_agent_type: str | None = None,
    ) -> bool:
        """内存级 owner 匹配（供第二级 MySQL 复核 / 测试向量复用）。"""
        if owner_user_id != self.user_id:
            return False
        if self.mode == "user_global":
            return owner_workspace_key is None
        if self.mode == "workspace":
            return bool(self.workspace_key) and owner_workspace_key == self.workspace_key
        if self.mode == "union":
            if owner_workspace_key is None:
                return True
            return owner_workspace_key == self.workspace_key
        if self.mode == "playbook":
            if owner_workspace_key is not None:
                return False
            if row_scope_type != "agent_playbook":
                return False
            return bool(self.agent_type) and row_agent_type == self.agent_type
        return False

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "user_id": self.user_id,
            "workspace_key": self.workspace_key,
            "agent_type": self.agent_type,
        }


class RetrievalRequest(FrozenModel):
    """单条 typed 检索请求（由 ContextPlan.retrieval_queries 显式转换）。"""

    query_text: str = Field(default="", max_length=2000)
    strategy: RetrievalStrategy = RetrievalStrategy.PLANNED
    source_family: str | None = None  # knowledge | memory | artifact
    # These filters are enforced by the authoritative MySQL recheck.  External
    # stores may return a wider candidate set, but no chunk outside this set
    # can enter the final evidence context.
    source_types: list[str] = Field(default_factory=list)
    source_public_ids: list[str] = Field(default_factory=list)
    document_public_ids: list[str] = Field(default_factory=list)
    source_versions: dict[str, str] = Field(default_factory=dict)
    top_k: int = Field(default=5, ge=1, le=50)
    rerank_strategy: RerankStrategy = RerankStrategy.WEIGHTED_RRF
    scope: RetrievalScopeFilter
    max_candidates: int | None = Field(default=None, ge=1)
    requested_final_limit: int | None = Field(default=None, ge=1)

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "query_text": self.query_text,
            "strategy": self.strategy.value if isinstance(self.strategy, RetrievalStrategy) else str(self.strategy),
            "source_family": self.source_family,
            "source_types": list(self.source_types),
            "source_public_ids": list(self.source_public_ids),
            "document_public_ids": list(self.document_public_ids),
            "source_versions": dict(self.source_versions),
            "top_k": self.top_k,
            "rerank_strategy": (
                self.rerank_strategy.value
                if isinstance(self.rerank_strategy, RerankStrategy)
                else str(self.rerank_strategy)
            ),
            "scope": self.scope.to_state_dict(),
        }
# auto-appended module-level note: retrieval 模型: RetrievalPlan / RetrievalHit / RetrievalTraceRow。
