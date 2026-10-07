"""Structured Dynamic Agent runtime schemas."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.utils.ids import generate_public_id


class DynamicStepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    SUPERSEDED = "superseded"
    WAITING_USER = "waiting_user"


class DynamicPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    action_type: str = Field(min_length=1)
    capability_key: str = Field(min_length=1)
    input_refs: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(min_length=1)
    status: DynamicStepStatus = DynamicStepStatus.PENDING


class DynamicPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(default_factory=lambda: generate_public_id("dyn_plan"), min_length=1)
    goal: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    status: str = "active"
    steps: list[DynamicPlanStep] = Field(min_length=1)


class DynamicObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    capability_key: str = Field(min_length=1)
    status: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    result_ref: str = Field(min_length=1)
    facts: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


# module-level note (auto-appended):
# Pydantic 模型: DynamicObservation / DynamicPlan / DynamicPlanStep / DynamicStepStatus。
# 关键约束: state TypedDict total=False。
