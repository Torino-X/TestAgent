"""Deterministic validation for Dynamic Agent plans."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.atomic_capability_registry import (
    AtomicCapabilityRegistry,
    AtomicSideEffect,
)

from .budgets import DynamicAgentBudgets
from .schemas import DynamicPlan


@dataclass(frozen=True, slots=True)
class PlanValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)


class DynamicPlanValidator:
    def __init__(
        self,
        registry: AtomicCapabilityRegistry,
        *,
        budgets: DynamicAgentBudgets | None = None,
    ) -> None:
        self._registry = registry
        self._budgets = budgets or DynamicAgentBudgets()

    def validate(self, plan: DynamicPlan, state: dict) -> PlanValidationResult:
        errors: list[str] = []
        if len(plan.steps) > self._budgets.max_plan_steps:
            errors.append("budget_exceeded:max_plan_steps")

        step_ids = [step.step_id for step in plan.steps]
        if len(step_ids) != len(set(step_ids)):
            errors.append("duplicate_step_id")

        known_steps = set(step_ids)
        for step in plan.steps:
            spec = self._registry.get(step.capability_key)
            if spec is None:
                errors.append(f"unknown_capability:{step.capability_key}")
                continue
            if not spec.planner_visible:
                errors.append(f"capability_not_planner_visible:{step.capability_key}")
            if spec.side_effect not in {AtomicSideEffect.READ, AtomicSideEffect.EXTERNAL_READ}:
                errors.append(f"side_effect_not_allowed:{step.capability_key}")
            if step.capability_key == "knowledge_search" and not self._knowledge_search_allowed(state):
                errors.append("retrieval_policy_disallows:knowledge_search")
            for dep in step.depends_on:
                if dep not in known_steps:
                    errors.append(f"unknown_dependency:{dep}")
            for ref in step.input_refs:
                if ref.startswith("attachment:") and not self._attachment_ref_allowed(ref, state):
                    errors.append(f"attachment_ref_not_allowed:{ref}")

        if self._has_cycle(plan):
            errors.append("cyclic_plan")
        return PlanValidationResult(valid=not errors, errors=errors)

    @staticmethod
    def _attachment_ref_allowed(ref: str, state: dict) -> bool:
        refs = {
            str(item.get("ref"))
            for item in list(state.get("attachment_refs") or [])
            if isinstance(item, dict)
        }
        return ref in refs if refs else True

    @staticmethod
    def _knowledge_search_allowed(state: dict) -> bool:
        retrieval_plan = state.get("retrieval_plan_snapshot")
        retrieval_plan = retrieval_plan if isinstance(retrieval_plan, dict) else {}
        maas = str(retrieval_plan.get("maas") or "").lower()
        if maas == "off":
            return False
        if maas in {"required", "auto"}:
            return True
        return str(state.get("knowledge_mode_snapshot") or "").upper() in {
            "MAAS_STRICT",
            "KNOWLEDGE_REQUIRED",
        }

    @staticmethod
    def _has_cycle(plan: DynamicPlan) -> bool:
        graph = {step.step_id: set(step.depends_on) for step in plan.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            for dep in graph.get(node, set()):
                if dep in graph and visit(dep):
                    return True
            visiting.remove(node)
            visited.add(node)
            return False

        return any(visit(node) for node in graph)


# module-level note (auto-appended):
# DynamicPlanValidator — 计划合法性校验。
# 关键约束: 不允许 plan 含越权工具。
