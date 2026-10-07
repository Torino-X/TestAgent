"""Preparation Agent Pydantic v2 schemas (Phase 2.3).

设计约束:
* 所有模型必须 JSON 可序列化 (Rule 10 — 不放 Session / LLMClient)
* 字段长度上限防 Chain-of-Thought 膨胀 (Rule 11)
* 严格分输入 / 内部决策 / 公开输出 三个 schema 层
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent_runtime._shared.public_narrative import (
    AgentPublicUpdateDraft,
    normalize_public_update,
)


# ── Decision action enum ─────────────────────────────────────────────────

Action = Literal["call_tool", "finish", "ask_user", "fail"]


class ClarificationOption(BaseModel):
    """A concise, user-facing choice proposed by the PreparationAgent."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=80)
    label: str = Field(max_length=100)
    description: str = Field(default="", max_length=180)


class ClarificationGap(BaseModel):
    """A bounded, LLM-owned gap that may require task-scoped user input."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(max_length=80)
    description: str = Field(max_length=240)
    severity: Literal["low", "medium", "high"] = "medium"
    selection_mode: Literal["single", "multiple"] = "single"
    # Kept optional for recovery paths created before selectable choices were
    # introduced. The graph supplies a bounded fallback before it reaches UI.
    options: List[ClarificationOption] = Field(default_factory=list, max_length=4)


class AgentDecision(BaseModel):
    """LLM 单步输出。Mode A (Structured Action) 强约束。

    字段:
    * action: 下一步动作 — call_tool / finish / ask_user / fail
    * tool_name: action=call_tool 时必填,必须在 PREPARATION_TOOL_WHITELIST 内
    * tool_arguments: call_tool 时必填,需通过 per-tool schema 校验
    * action_reason: 内部决策短句(≤200 字),仅用于日志/审计/调试,不入公开载荷
    * decision_summary: 内部决策摘要,审计用;不入公开事件载荷
    * public_update: 用户可见说明 (PublicSummary);event_emitter 仅取此字段
    * expected_result: 自检用 (e.g. "应返回 ≥3 chunk")
    * confidence: 0.0-1.0 浮点,None 表示不确定
    """

    model_config = ConfigDict(extra="forbid")

    action: Action
    tool_name: Optional[str] = Field(default=None, max_length=80)
    tool_arguments: Optional[Dict[str, Any]] = None
    action_reason: Optional[str] = Field(default=None, max_length=200)
    decision_summary: str = Field(max_length=500)
    public_update: Optional[str] = Field(default=None, max_length=240)
    observation_update: Optional[AgentPublicUpdateDraft] = None
    decision_update: Optional[AgentPublicUpdateDraft] = None
    clarification_gaps: List[ClarificationGap] = Field(default_factory=list, max_length=3)
    expected_result: Optional[str] = Field(default=None, max_length=240)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    @field_validator("observation_update", "decision_update", mode="before")
    @classmethod
    def _normalize_public_update(cls, value: Any) -> AgentPublicUpdateDraft | None:
        # Narrative output is additive. A malformed optional narrative must not
        # make the core tool decision unusable.
        return normalize_public_update(value)

    @field_validator("clarification_gaps", mode="before")
    @classmethod
    def _normalize_empty_clarification_gaps(cls, value: Any) -> Any:
        """Accept a model's JSON ``null`` as the empty list for non-clarification actions.

        The semantic action validation below still rejects ``ask_user`` when no
        structured gap is supplied.  This only keeps an otherwise valid
        ``call_tool`` / ``finish`` decision from falling into the legacy
        fallback because a provider serialized an optional list as null.
        """
        return [] if value is None else value

    @model_validator(mode="after")
    def _require_explicit_gaps_for_user_clarification(self) -> "AgentDecision":
        """The LLM owns gap judgment; the runtime only requires it to be structured."""
        if self.action == "ask_user" and not self.clarification_gaps:
            raise ValueError("ask_user requires at least one clarification_gaps item")
        if self.action != "ask_user" and self.clarification_gaps:
            raise ValueError("clarification_gaps are only valid for ask_user")
        return self


# ── Knowledge / Gap / Question ──────────────────────────────────────────


class KnowledgeEvidence(BaseModel):
    """单条 KB 检索证据。snippet 截断防 checkpoint 膨胀 (Risk #4)。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(max_length=240)
    snippet: str = Field(max_length=400)
    source: Optional[str] = Field(default=None, max_length=120)
    relevance: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class RequirementGap(BaseModel):
    """LLM 发现的缺口。仅描述,不修改用户输入。"""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(max_length=80)
    description: str = Field(max_length=240)
    severity: Literal["low", "medium", "high"] = "medium"
    selection_mode: Literal["single", "multiple"] = "single"
    options: List[ClarificationOption] = Field(default_factory=list, max_length=4)


class UserQuestion(BaseModel):
    """ask_user 时向用户追问。"""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(max_length=80)
    question: str = Field(max_length=240)


# ── Budget snapshot ─────────────────────────────────────────────────────


# Re-export from _shared/ to keep one canonical BudgetState (Phase 2.4 ADR-2.4-1).
from app.agent_runtime._shared.budget import BudgetState  # noqa: E402,F401


# ── Public output ────────────────────────────────────────────────────────


class PublicSummary(BaseModel):
    """对外可见的最终说明。所有字段 max_length,无 raw LLM 文本 (Rule 11)。"""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(max_length=120)
    detail: Optional[str] = Field(default=None, max_length=480)


# ── Top-level result ─────────────────────────────────────────────────────


class PreparationResult(BaseModel):
    """Preparation Agent 终产物,写入 TestPlanGraphState.preparation_result。"""

    model_config = ConfigDict(extra="forbid")

    information_sufficient: bool
    knowledge_search_used: bool
    queries: List[str] = Field(default_factory=list)
    evidence: List[KnowledgeEvidence] = Field(default_factory=list)
    requirement_gaps: List[RequirementGap] = Field(default_factory=list)
    user_questions: List[UserQuestion] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    public_summary: PublicSummary
    fallback_reason: Optional[str] = Field(default=None, max_length=240)
    budget_state: BudgetState


__all__ = [
    "Action",
    "ClarificationOption",
    "ClarificationGap",
    "AgentDecision",
    "KnowledgeEvidence",
    "RequirementGap",
    "UserQuestion",
    "BudgetState",
    "PublicSummary",
    "PreparationResult",
]


# module-level note (auto-appended):
# Pydantic 模型: AgentDecision / PreparationResult / UserQuestion 等。
# 关键约束: 不依赖 prompt 字面值(structure 跨版本兼容)。
