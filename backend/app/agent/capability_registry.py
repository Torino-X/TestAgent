"""Capability registry for request-understanding routing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    capability_key: str
    enabled: bool
    executor_type: str
    legacy_task_type: str | None = None
    unavailable_reason: str = ""
    fixed_workflow: str | None = None
    allow_dynamic_fallback: bool = False


class CapabilityRegistry:
    def __init__(self, specs: list[CapabilitySpec]) -> None:
        self._by_key = {spec.capability_key: spec for spec in specs}

    @classmethod
    def default(
        cls,
        *,
        maas_enabled: bool = False,
        vision_enabled: bool = True,
        test_case_enabled: bool = False,
        defect_enabled: bool = False,
    ) -> "CapabilityRegistry":
        return cls(
            [
                CapabilitySpec("general_chat", True, "chat"),
                CapabilitySpec(
                    "document_qa",
                    True,
                    "chat",
                    allow_dynamic_fallback=True,
                ),
                CapabilitySpec(
                    "enterprise_knowledge_qa",
                    maas_enabled,
                    "chat",
                    unavailable_reason="enterprise_knowledge_unavailable",
                ),
                CapabilitySpec(
                    "image_qa",
                    vision_enabled,
                    "chat",
                    unavailable_reason="vision_unavailable",
                ),
                CapabilitySpec(
                    "test_plan_generation",
                    True,
                    "agent_task",
                    legacy_task_type="test_plan_generation",
                    fixed_workflow="test_plan/v3",
                ),
                CapabilitySpec(
                    "test_case_generation",
                    test_case_enabled,
                    "agent_task",
                    legacy_task_type="test_case_generation",
                    unavailable_reason="test_case_generation_unavailable",
                ),
                CapabilitySpec(
                    "test_plan_incremental",
                    True,
                    "agent_task",
                    legacy_task_type="incremental_test_plan",
                    fixed_workflow="test_plan/incremental",
                ),
                CapabilitySpec(
                    "defect_analysis",
                    defect_enabled,
                    "agent_task",
                    legacy_task_type="defect_analysis",
                    unavailable_reason="defect_analysis_unavailable",
                ),
                CapabilitySpec("active_task_action", True, "task_action"),
            ]
        )

    def get(self, capability_key: str) -> CapabilitySpec | None:
        return self._by_key.get(capability_key)


# 模块定位:能力注册表(Request Understanding 路由用)
#
# 注册 "能力名" → 可执行 Provider 类。
# RequestUnderstanding 输出 capability 路由到这里,挑出对应 Provider 跑。
#
# 链路:
#   RequestUnderstandingResult.capability
#     → CapabilityRegistry.get(capability_name) → Provider
#     → Provider.execute(...)
#
# 关键约束:
#   - 注册必须 singleton,不允许同一 capability 名注册多次;
#   - 未注册能力返回 None(由调用方降级);
#   - 与 atomic_capability_registry 区分:
#     * atomic = Dynamic Agent planner 用的"原语能力"
#     * capability = 高层路由用的"语义能力"
