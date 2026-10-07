"""Incremental Agent Pydantic v2 schemas (Phase 2.5).

设计约束 (镜像 preparation/schemas.py + repair/schemas.py):
* 所有模型必须 JSON 可序列化 (Rule 10)
* 字段长度上限防 Chain-of-Thought 膨胀 (Rule 11)
* 严格分输入 / 内部决策 / 公开输出 三个 schema 层

Phase 2.5 特殊点:

* ``ExistingArtifactRef`` — 用户提供的"上次已完成 artifact"引用;Incremental
  Agent 的所有读路径从这里开始(source_artifact_id / version_no /
  section_package / review_result / last_format_check_result)。
* ``ModificationScope`` — 用户提的修改范围(目标 section_ids + kind +
  request_text);LLM 在此基础上派生 IncrementalDecision。
* ``IncrementalIntent`` — ``intent_router`` 写入 state 的"用户意图对象",
  由 IntentRouter 在 RESULT_MODIFICATION route=agent_task 时填充。
* ``IncrementalDecision`` — LLM 单步输出(JSON)。比 RepairDecision 多了
  ``target_section_ids`` 必须 ⊆ ``scope.allowed_section_ids`` 的硬约束。
* ``IncrementalResult`` — 终产物,写入 state.repair_result(同字段名复用
  为 ``incremental_result``);main graph 读 ``success`` 决定 finalize。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent_runtime._shared.public_narrative import (
    AgentPublicUpdateDraft,
    normalize_public_update,
)

# Re-export from _shared for cross-package shared budget state.
from app.agent_runtime._shared.budget import BudgetState  # noqa: E402,F401


# ── Decision action enum ─────────────────────────────────────────────

Action = Literal["call_tool", "finish", "fail", "ask_user"]


# ── ExistingArtifactRef — 用户提供的"上次已完成 artifact" 引用 ─────


class ExistingArtifactRef(BaseModel):
    """``payload.existing_artifact`` 形状。

    Phase 2.5 字段(都来自 ``Artifact`` 表的对应列):

    * ``artifact_public_id`` — 必填,Artifact.public_id
    * ``task_public_id`` — 上次任务的 public_id,用于追溯 source_artifact_id
    * ``version_no`` — 上次 artifact 的版本号(Phase 1 已落库)
    * ``source_artifact_id`` — 上上次 artifact 的 internal_id(若存在)
    * ``superseded_artifact_ids`` — 整条链上被取代的 artifact internal_id
      列表(从 task_context_json.superseded_artifact_ids 读)
    * ``section_package`` — 上次生成时的 section_package 字典快照
    * ``review_result`` — 上次生成的 review_result 字典快照
    * ``last_format_check_result`` — 上次生成的 format_check_result 字典快照
    """

    model_config = ConfigDict(extra="forbid")

    artifact_public_id: str = Field(min_length=1, max_length=64)
    task_public_id: Optional[str] = Field(default=None, max_length=64)
    version_no: int = Field(default=1, ge=1)
    source_artifact_id: Optional[int] = Field(default=None, ge=1)
    superseded_artifact_ids: List[int] = Field(default_factory=list)
    section_package: Dict[str, Any] = Field(default_factory=dict)
    review_result: Dict[str, Any] = Field(default_factory=dict)
    last_format_check_result: Dict[str, Any] = Field(default_factory=dict)


# ── ModificationScope — 用户提出的修改范围(由 IntentRouter 写入) ──


class ModificationScope(BaseModel):
    """Incremental 任务的"用户语义边界"。

    * ``kind`` ∈ {modify_section, extend_scope, adjust_table, re_review,
      re_export}
    * ``target_section_ids`` — 用户明确点名要改的 sections;allow_extra=False
      时 LLM 不能扩展
    * ``request_text`` — 用户原始要求,直接进入 prompt
    * ``locked_section_ids`` — 用户已锁定的 section 列表(scope_guard 守门)
    * ``new_artifact_idempotency_key`` — 客户端生成的幂等 key(可选;缺省由
      框架用 ``(task_public_id, source_artifact_id, request_text)`` 派生)
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "modify_section",
        "extend_scope",
        "adjust_table",
        "re_review",
        "re_export",
    ]
    target_section_ids: List[str] = Field(default_factory=list)
    request_text: str = Field(min_length=1, max_length=2000)
    locked_section_ids: List[str] = Field(default_factory=list)
    allow_extra_sections: bool = False
    new_artifact_idempotency_key: Optional[str] = Field(
        default=None, max_length=128,
    )


# ── IncrementalIntent — IntentRouter 写入 state 的"用户意图对象" ───


class IncrementalIntent(BaseModel):
    """IntentRouter 在 ``route=agent_task + intent=result_modification`` 时
    写入 ``state.incremental_intent`` 的对象。

    Phase 2.5 (决策 A — 复用 RESULT_MODIFICATION + 条件 gate):

    * ``detected_by`` 固定为 ``"intent_router.result_modification"``
    * ``original_intent`` 写回用户原始 IntentType 值(result_modification)
    * ``confidence`` 是 IntentRouter 给出的 confidence
    * ``existing_artifact`` 由 IntentRouter 从会话最近已完成 artifact
      派生(``IntentContext.last_completed_artifact_public_id``)
    * ``scope`` 由 IntentRouter 用启发式规则从 user_content 派生
      (LLM 不参与,防止再次分类)
    """

    model_config = ConfigDict(extra="forbid")

    detected_by: Literal["intent_router.result_modification"] = (
        "intent_router.result_modification"
    )
    original_intent: Literal["result_modification"] = "result_modification"
    confidence: float = Field(ge=0.0, le=1.0)
    existing_artifact: ExistingArtifactRef
    scope: ModificationScope
    # 用户原始消息;审计用
    raw_user_message: str = Field(max_length=2000)


# ── IncrementalDecision — LLM 单步输出 (Mode A — Structured Action) ──


class IncrementalDecision(BaseModel):
    """LLM 单步 Incremental Agent 输出 JSON。

    与 RepairDecision 的差异:

    * ``target_section_ids`` **必须** ⊆ ``scope.allowed_section_ids`` —
      scope_guard 在决策 filter 阶段强制;若 LLM 超出范围 → action=fail
    * ``action=ask_user`` 也允许(用户原始意图不明,LLM 主动询问)
    * ``scope_kind`` 必须回传,与 IntentRouter 写入的 ``IncrementalIntent.scope.kind``
      对齐(防止 LLM 擅自换任务类型)
    """

    model_config = ConfigDict(extra="forbid")

    action: Action
    tool_name: Optional[str] = Field(default=None, max_length=80)
    tool_arguments: Optional[Dict[str, Any]] = None
    target_section_ids: List[str] = Field(default_factory=list)
    suggested_strategy: Optional[str] = Field(default=None, max_length=80)
    scope_kind: Optional[Literal[
        "modify_section",
        "extend_scope",
        "adjust_table",
        "re_review",
        "re_export",
    ]] = None
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


# ── Public / Budget ───────────────────────────────────────────────


class PublicSummary(BaseModel):
    """对外可见的最终说明。所有字段 max_length,无 raw LLM 文本 (Rule 11)。"""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(max_length=120)
    detail: Optional[str] = Field(default=None, max_length=480)


# ── Top-level result ───────────────────────────────────────────────


class IncrementalResult(BaseModel):
    """Incremental Agent 终产物,写入 ``state.incremental_result``。

    主图 ``incremental_subgraph_node`` 读 ``success`` 决定 finalize_task
    还是兜底(导出走 ``WordExportTool`` 时显式把 version_no=2 落库)。
    """

    model_config = ConfigDict(extra="forbid")

    success: bool
    new_artifact_public_id: Optional[str] = Field(default=None, max_length=64)
    new_artifact_version_no: Optional[int] = Field(default=None, ge=1)
    superseded_artifact_public_ids: List[str] = Field(default_factory=list)
    modified_section_ids: List[str] = Field(default_factory=list)
    tool_calls_used: int = Field(ge=0, default=0)
    rounds_used: int = Field(ge=0, default=0)
    public_summary: PublicSummary
    fallback_reason: Optional[str] = Field(default=None, max_length=240)
    budget_state: BudgetState


__all__ = [
    "Action",
    "ExistingArtifactRef",
    "ModificationScope",
    "IncrementalIntent",
    "IncrementalDecision",
    "PublicSummary",
    "BudgetState",
    "IncrementalResult",
]


# module-level note (auto-appended):
# Pydantic 模型: IncrementalDecision / IncrementalIntent / IncrementalResult / ModificationScope。
# 关键约束: state schema_version 升 5。
