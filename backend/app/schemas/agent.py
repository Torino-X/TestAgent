"""Agent & task schemas."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


# ── Task ───────────────────────────────────────────────────────────────

class TaskDetail(BaseModel):
    task_id: str
    task_type: str = "test_plan_generation"
    status: str = "created"
    plan: Optional[List[Dict[str, Any]]] = None
    task_context: Optional[Dict[str, Any]] = None
    review_result: Optional[Dict[str, Any]] = None


class TaskEventSummary(BaseModel):
    event_id: str
    event_type: str
    title: Optional[str] = None
    content: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    created_at: str


class TaskEventList(BaseModel):
    events: List[TaskEventSummary]
    total: int


# ── Confirmation ───────────────────────────────────────────────────────

class SectionActionItem(BaseModel):
    section_id: str
    action: str = "ai_generate"  # ai_generate | keep_template | manual_fill | skip


class ConfirmSectionsRequest(BaseModel):
    sections: List[SectionActionItem]

    @model_validator(mode="before")
    @classmethod
    def unwrap_legacy_response(cls, value: Any) -> Any:
        """Accept the legacy confirmation envelope without changing the API contract."""
        if not isinstance(value, dict) or "sections" in value:
            return value
        response = value.get("response")
        if not isinstance(response, dict) or "sections" not in response:
            return value
        normalized = dict(value)
        normalized["sections"] = response["sections"]
        return normalized


class ConfirmResponse(BaseModel):
    task_id: str
    status: str


class PreparationClarificationRequest(BaseModel):
    """Task-scoped answers for gaps that remained after two RAG rounds."""

    answers: Dict[str, str] = Field(default_factory=dict)
    conservative_gap_ids: List[str] = Field(default_factory=list)


# ── Retry ───────────────────────────────────────────────────────────────

class RetryTaskRequest(BaseModel):
    retry_mode: str = "from_failed_step"  # from_failed_step | from_beginning
    user_instruction: Optional[str] = None  # 可选：覆盖原 prompt（仅 from_failed_step 生效）


class RetryTaskResponse(BaseModel):
    old_task_id: str
    new_task_id: str
    status: str
    events_url: str


# ── F025-ext: format-loss confirmation ───────────────────────────────


class FormatLossDecisionRequest(BaseModel):
    """Body for ``POST /agent/tasks/{task_id}/format-loss-decision``.

    ``decision`` is one of:

    * ``"accept"`` — user accepts the loss; the orchestrator
      proceeds with the current artifact.
    * ``"retry"``  — user wants a fresh export with the lowest
      fidelity setting.  The orchestrator re-runs ``WordExportTool``
      and re-checks the format; if losses still appear, the loop
      ends silently (the user is not prompted again).
    """

    decision: str  # "accept" | "retry"
    note: Optional[str] = None


class FormatLossDecisionResponse(BaseModel):
    task_id: str
    decision: str
    next_action: str  # "complete" | "re_export"
    fallback_to_accept: bool = False


# ── Artifact ───────────────────────────────────────────────────────────

class ArtifactSummary(BaseModel):
    artifact_id: str
    artifact_type: str
    file_name: str
    file_ext: str
    file_size: Optional[int] = None
    status: str
    version_no: int
    created_at: str


class ArtifactListResponse(BaseModel):
    artifacts: List[ArtifactSummary]
    total: int
