"""Route request-understanding results to executable capabilities."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.agent.capability_registry import CapabilityRegistry, CapabilitySpec
from app.agent.enums import MessageRoute
from app.agent.request_understanding import RequestClass, RequestUnderstandingResult


class CapabilityDecisionRoute(StrEnum):
    CHAT = "chat"
    AGENT_TASK = "agent_task"
    DYNAMIC_AGENT = "dynamic_agent"
    TASK_ACTION = "task_action"
    CLARIFICATION = "clarification"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"


class ExecutionMode(StrEnum):
    DIRECT_CHAT = "direct_chat"
    FIXED_WORKFLOW = "fixed_workflow"
    DYNAMIC_AGENT = "dynamic_agent"
    EXISTING_TASK_ACTION = "existing_task_action"
    CLARIFICATION = "clarification"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"


class CapabilityDecision(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    route: CapabilityDecisionRoute
    execution_mode: ExecutionMode
    target_capability: str
    legacy_route: MessageRoute
    supported: bool = True
    reason: str = ""
    capability: CapabilitySpec | None = None


class CapabilityRouter:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def route(self, understanding: RequestUnderstandingResult) -> CapabilityDecision:
        if understanding.request_class == RequestClass.TASK_ACTION:
            return CapabilityDecision(
                route=CapabilityDecisionRoute.TASK_ACTION,
                execution_mode=ExecutionMode.EXISTING_TASK_ACTION,
                target_capability=understanding.target_capability,
                legacy_route=MessageRoute.EXISTING_TASK_ACTION,
                reason=understanding.reason,
            )

        if understanding.request_class == RequestClass.CLARIFICATION:
            return CapabilityDecision(
                route=CapabilityDecisionRoute.CLARIFICATION,
                execution_mode=ExecutionMode.CLARIFICATION,
                target_capability=understanding.target_capability,
                legacy_route=MessageRoute.CLARIFY,
                reason=understanding.reason,
            )

        capability = self._registry.get(understanding.target_capability)
        if capability is None:
            return CapabilityDecision(
                route=CapabilityDecisionRoute.CAPABILITY_UNAVAILABLE,
                execution_mode=ExecutionMode.CAPABILITY_UNAVAILABLE,
                target_capability=understanding.target_capability,
                legacy_route=MessageRoute.UNSUPPORTED,
                supported=False,
                reason="capability_not_registered",
            )

        if understanding.request_class == RequestClass.CHAT:
            if self._should_use_dynamic_agent(capability, understanding):
                return CapabilityDecision(
                    route=CapabilityDecisionRoute.DYNAMIC_AGENT,
                    execution_mode=ExecutionMode.DYNAMIC_AGENT,
                    target_capability=understanding.target_capability,
                    legacy_route=MessageRoute.AGENT_TASK,
                    supported=True,
                    reason=understanding.reason or "dynamic_agent_fallback",
                    capability=capability,
                )
            # A disabled enterprise KB capability is still allowed to travel
            # through chat; strict-mode availability is enforced by the
            # retrieval planner/answer path with a precise user-facing message.
            return CapabilityDecision(
                route=CapabilityDecisionRoute.CHAT,
                execution_mode=ExecutionMode.DIRECT_CHAT,
                target_capability=understanding.target_capability,
                legacy_route=MessageRoute.CHAT_REPLY,
                supported=True,
                reason=understanding.reason,
                capability=capability,
            )

        if not capability.enabled:
            return CapabilityDecision(
                route=CapabilityDecisionRoute.CAPABILITY_UNAVAILABLE,
                execution_mode=ExecutionMode.CAPABILITY_UNAVAILABLE,
                target_capability=understanding.target_capability,
                legacy_route=MessageRoute.UNSUPPORTED,
                supported=False,
                reason=capability.unavailable_reason or "capability_disabled",
                capability=capability,
            )

        if capability.executor_type == "agent_task":
            return CapabilityDecision(
                route=CapabilityDecisionRoute.AGENT_TASK,
                execution_mode=(
                    ExecutionMode.FIXED_WORKFLOW
                    if capability.fixed_workflow
                    else ExecutionMode.DYNAMIC_AGENT
                ),
                target_capability=understanding.target_capability,
                legacy_route=MessageRoute.AGENT_TASK,
                supported=True,
                reason=understanding.reason,
                capability=capability,
            )

        return CapabilityDecision(
            route=CapabilityDecisionRoute.CHAT,
            execution_mode=ExecutionMode.DIRECT_CHAT,
            target_capability=understanding.target_capability,
            legacy_route=MessageRoute.CHAT_REPLY,
            supported=True,
            reason=understanding.reason,
            capability=capability,
        )

    @staticmethod
    def _should_use_dynamic_agent(
        capability: CapabilitySpec,
        understanding: RequestUnderstandingResult,
    ) -> bool:
        if not capability.enabled or not capability.allow_dynamic_fallback:
            return False
        if understanding.target_capability != "document_qa":
            return False
        if understanding.operation not in {"summarize", "analyze", "extract", "compare"}:
            return False
        return any(ext == ".docx" for ext in understanding.attachment_file_exts)


# 模块定位:RequestUnderstanding 结果路由到可执行 capability
#
# 链路:
#   RequestUnderstandingResult → CapabilityRouter.route(result)
#     → Provider / Script / Plan 选定
#     → CapabilityExecutor.dispatch(provider, params)
#
# 能力集样例:
#   - parse_requirement / parse_template
#   - generate_test_plan
#   - search_knowledge
#   - export_word
#
# 关键约束:
#   - 不允许 fallback 到 default(必须显式指定 capability);
#   - 同 capability 多版本注册 → 取最新(版本号在 prefix);
#   - 失败 → CapabilityNotFoundError(由 MessageService 转为 ask_for_files)。
