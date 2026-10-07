"""Repair Agent Pydantic v2 schemas (Phase 2.4).

设计约束 (镜像 preparation/schemas.py):
* 所有模型必须 JSON 可序列化 (Rule 10 — 不放 Session / LLMClient)
* 字段长度上限防 Chain-of-Thought 膨胀 (Rule 11)
* 严格分输入 / 内部决策 / 公开输出 三个 schema 层
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent_runtime._shared.public_narrative import (
    AgentPublicUpdateDraft,
    normalize_public_update,
)

# Re-export from _shared for cross-package shared budget state (ADR-2.4-1).
from app.agent_runtime._shared.budget import BudgetState  # noqa: E402,F401


# ── Decision action enum ─────────────────────────────────────────────

Action = Literal["call_tool", "finish", "fail"]


# ── ReviewIssue (标准化结果审查问题,镜像 tools/result_review_tool.py) ──


class ReviewIssue(BaseModel):
    """单条标准化 review issue(从 ResultReviewTool 输出解析)。

    Phase 2.4 — ADR-2.4-3 bug-fix #1: ``ResultReviewTool._evaluate_rules`` 现在
    每条 issue 都包含这些字段,本 schema 是 Pydantic v2 收口与下游消费者
    强校验的形状。
    """

    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(max_length=200)
    rule_id: str = Field(max_length=80)
    kind: str = Field(max_length=80)
    severity: Literal["warning", "block"] = "block"
    section_id: Optional[str] = Field(default=None, max_length=120)
    field_path: Optional[str] = Field(default=None, max_length=240)
    message: str = Field(max_length=480)
    evidence: Optional[str] = Field(default=None, max_length=400)
    expected_rule: Optional[str] = Field(default=None, max_length=240)
    repairable: bool = True
    suggested_strategy: Optional[str] = Field(default=None, max_length=80)
    # Phase 2.9A.X：ResultReviewTool 透传的 LLM-actionable 修正指令，
    # 由 TestPlanRegenTool._build_prompt 直接注入到 LLM 重写 prompt。
    fix_instruction: Optional[str] = Field(default=None, max_length=800)


class RepairDecision(BaseModel):
    """LLM 单步 Repair 输出 (Mode A — Structured Action) JSON。

    与 ``Preparation.schemas.AgentDecision`` 的区别在于多了
    ``target_issue_ids`` / ``target_section_ids`` / ``suggested_strategy``:
    Repair Agent 必须显式声明本次修复针对哪些 issue 与 section,以便
    ``scope_guard`` 与 ``issue_parser`` 验证最小修复范围。
    """

    model_config = ConfigDict(extra="forbid")

    action: Action
    tool_name: Optional[str] = Field(default=None, max_length=80)
    tool_arguments: Optional[Dict[str, Any]] = None
    target_issue_ids: List[str] = Field(default_factory=list)
    target_section_ids: List[str] = Field(default_factory=list)
    suggested_strategy: Optional[str] = Field(default=None, max_length=80)
    decision_summary: str = Field(max_length=500)
    public_update: Optional[str] = Field(default=None, max_length=240)
    observation_update: Optional[AgentPublicUpdateDraft] = None
    decision_update: Optional[AgentPublicUpdateDraft] = None
    expected_result: Optional[str] = Field(default=None, max_length=240)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    @field_validator("observation_update", "decision_update", mode="before")
    @classmethod
    def _normalize_public_update(cls, value: Any) -> AgentPublicUpdateDraft | None:
        return normalize_public_update(value)


# ── Knowledge / Public output ────────────────────────────────────────


class KnowledgeEvidence(BaseModel):
    """单条 KB 检索证据(snippet 截断防 checkpoint 膨胀 — Risk #4)。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(max_length=240)
    snippet: str = Field(max_length=400)
    source: Optional[str] = Field(default=None, max_length=120)
    relevance: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class PublicSummary(BaseModel):
    """对外可见的最终说明。所有字段 max_length,无 raw LLM 文本 (Rule 11)。"""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(max_length=120)
    detail: Optional[str] = Field(default=None, max_length=480)


# ── Top-level result ────────────────────────────────────────────────


class RepairResult(BaseModel):
    """Repair Agent 终产物,写入 ``TestPlanGraphState.repair_result``。

    主图 ``repair_subgraph_node`` 读 ``review_passed`` 决定 ``route_after_review``
    走 ``prepare_export`` 还是兜底。
    """

    model_config = ConfigDict(extra="forbid")

    review_passed: bool
    issues_resolved: List[str] = Field(default_factory=list)
    issues_remaining: List[str] = Field(default_factory=list)
    remaining_issue_details: List[Dict[str, Any]] = Field(default_factory=list)
    tool_calls_used: int = Field(ge=0, default=0)
    rounds_used: int = Field(ge=0, default=0)
    modified_section_ids: List[str] = Field(default_factory=list)
    knowledge_evidence: List[KnowledgeEvidence] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    public_summary: PublicSummary
    fallback_reason: Optional[str] = Field(default=None, max_length=240)
    budget_state: BudgetState


__all__ = [
    "Action",
    "ReviewIssue",
    "RepairDecision",
    "KnowledgeEvidence",
    "PublicSummary",
    "BudgetState",
    "RepairResult",
]


# module-level note (auto-appended):
# Pydantic 模型: RepairDecision / RepairResult / ReviewIssue 等。
# 关键约束: 字段集与 PreparationDecision 不同(共线 工厂模式)。
