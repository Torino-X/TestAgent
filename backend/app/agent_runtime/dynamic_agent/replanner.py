"""Dynamic Agent replanner skeleton."""

from __future__ import annotations

from .schemas import DynamicPlan, DynamicPlanStep, DynamicStepStatus


class DynamicReplanner:
    def replan(self, plan: DynamicPlan, *, gaps: list[str]) -> DynamicPlan:
        steps: list[DynamicPlanStep] = []
        for step in plan.steps:
            if step.status == DynamicStepStatus.COMPLETED:
                steps.append(step)
            else:
                steps.append(step.model_copy(update={"status": DynamicStepStatus.SUPERSEDED}))

        next_idx = len(steps) + 1
        steps.append(
            DynamicPlanStep(
                step_id=f"step_{next_idx}",
                title="补充分析缺口",
                action_type="analysis",
                capability_key="evidence_analysis",
                input_refs=["state:observations"],
                depends_on=[step.step_id for step in steps if step.status == DynamicStepStatus.COMPLETED],
                success_criteria=gaps or ["analysis_addresses_user_goal"],
            )
        )
        return DynamicPlan(
            plan_id=plan.plan_id,
            goal=plan.goal,
            revision=plan.revision + 1,
            status="active",
            steps=steps,
        )


# module-level note (auto-appended):
# DynamicReplanner — 重规划(plan 失败时)。
# 关键约束: 重规划轮数受 budgets 限制。
