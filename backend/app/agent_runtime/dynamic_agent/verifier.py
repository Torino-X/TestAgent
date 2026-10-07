"""Dynamic Agent goal verification skeleton."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class VerificationDecision(StrEnum):
    COMPLETE = "COMPLETE"
    CONTINUE = "CONTINUE"
    REPLAN = "REPLAN"
    NEED_USER = "NEED_USER"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    decision: VerificationDecision
    gaps: list[str] = field(default_factory=list)
    user_safe_reason: str = ""


class DynamicVerifier:
    def verify(self, state: dict) -> VerificationResult:
        plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
        steps = list(plan.get("steps") or [])
        if not steps:
            return VerificationResult(VerificationDecision.FAIL, ["plan_missing"])

        clarification = state.get("clarification")
        if state.get("awaiting_user") or isinstance(clarification, dict):
            question = ""
            if isinstance(clarification, dict):
                question = str(clarification.get("question") or "").strip()
            return VerificationResult(
                VerificationDecision.NEED_USER,
                ["user_input_required"],
                user_safe_reason=question or "User input is required to continue.",
            )

        observations = list(state.get("observations") or [])
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            facts = observation.get("facts")
            if isinstance(facts, dict) and facts.get("error_code") == "CAPABILITY_HANDLER_NOT_CONFIGURED":
                return VerificationResult(
                    VerificationDecision.FAIL,
                    ["capability_handler_not_configured"],
                )

        failed = [step for step in steps if step.get("status") == "failed"]
        if failed:
            return VerificationResult(VerificationDecision.REPLAN, ["step_failed"])

        pending = [
            step for step in steps
            if step.get("status") not in {"completed", "skipped", "superseded"}
        ]
        if pending:
            return VerificationResult(VerificationDecision.CONTINUE, ["steps_pending"])

        if not observations:
            return VerificationResult(VerificationDecision.REPLAN, ["observation_missing"])
        return VerificationResult(VerificationDecision.COMPLETE)


# module-level note (auto-appended):
# DynamicVerifier + VerificationDecision — 验证步结果。
# 关键约束: 失败 → re-plan,不允许 silent skip。
