"""Dynamic Agent planner."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.agent.atomic_capability_registry import AtomicCapabilityRegistry
from app.llm.errors import LLMProfileParseError
from app.llm.task_profiles import DYNAMIC_AGENT_PLANNER_PROFILE

from .schemas import DynamicPlan, DynamicPlanStep

logger = logging.getLogger(__name__)

_PLANNER_LLM_MAX_ATTEMPTS = 3


class DynamicPlanner:
    def __init__(self, registry: AtomicCapabilityRegistry) -> None:
        self._registry = registry

    async def aplan(self, state: dict, *, runtime_context: Any = None) -> DynamicPlan:
        bridge = getattr(runtime_context, "context_llm_invoker", None)
        fallback_reason = "context_bridge_missing"
        if (
            bridge is not None
            and hasattr(bridge, "generate")
            and getattr(bridge, "available", True)
        ):
            fallback_reason = "llm_planner_empty_response"
            last_exc: Exception | None = None
            for attempt in range(1, _PLANNER_LLM_MAX_ATTEMPTS + 1):
                try:
                    result = await bridge.generate(
                        user_id=getattr(runtime_context, "user_internal_id", 0),
                        call_site="dynamic_agent.planner",
                        llm_task_profile=DYNAMIC_AGENT_PLANNER_PROFILE,
                        current_node="create_plan",
                        current_goal=str(state.get("goal") or state.get("dynamic_goal") or ""),
                        task_state_ref=self._planner_state_ref(state, attempt=attempt),
                        output_contract="dynamic_plan_json",
                        user_content=_planner_user_content(state, attempt=attempt),
                        conversation_id=getattr(runtime_context, "conversation_internal_id", None),
                        task_id=str(state.get("task_id") or getattr(runtime_context, "task_internal_id", "")),
                        runtime_context=runtime_context,
                    )
                    value = getattr(result, "value", None) if result is not None else None
                    if value is not None:
                        return DynamicPlan.model_validate(value)
                    fallback_reason = "llm_planner_empty_response"
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    fallback_reason = f"llm_planner_exception:{type(exc).__name__}"
                    if not _is_retryable_planner_contract_error(exc):
                        logger.warning(
                            "FALLBACK_USED | component=dynamic_agent.planner | "
                            "from=llm_planner | to=deterministic_planner | reason=%s | "
                            "task_id=%s | target_capability=%s | operation=%s | attempt=%s | err=%s",
                            fallback_reason,
                            str(state.get("task_id") or getattr(runtime_context, "task_internal_id", "")),
                            str(state.get("target_capability") or ""),
                            str(state.get("operation") or ""),
                            attempt,
                            str(exc)[:300],
                        )
                        return self.plan(state)
                if attempt < _PLANNER_LLM_MAX_ATTEMPTS:
                    logger.warning(
                        "PLANNER_RETRY | component=dynamic_agent.planner | "
                        "reason=%s | task_id=%s | attempt=%s/%s",
                        fallback_reason,
                        str(state.get("task_id") or getattr(runtime_context, "task_internal_id", "")),
                        attempt,
                        _PLANNER_LLM_MAX_ATTEMPTS,
                    )
                    continue
                logger.warning(
                    "FALLBACK_USED | component=dynamic_agent.planner | "
                    "from=llm_planner | to=deterministic_planner | reason=%s | "
                    "task_id=%s | target_capability=%s | operation=%s | attempts=%s | err=%s",
                    fallback_reason,
                    str(state.get("task_id") or getattr(runtime_context, "task_internal_id", "")),
                    str(state.get("target_capability") or ""),
                    str(state.get("operation") or ""),
                    _PLANNER_LLM_MAX_ATTEMPTS,
                    str(last_exc)[:300] if last_exc is not None else fallback_reason,
                )
                return self.plan(state)
        elif bridge is not None and not hasattr(bridge, "generate"):
            fallback_reason = "context_bridge_generate_missing"
        elif bridge is not None:
            fallback_reason = "context_bridge_unavailable"

        logger.warning(
            "FALLBACK_USED | component=dynamic_agent.planner | "
            "from=llm_planner | to=deterministic_planner | reason=%s | "
            "task_id=%s | target_capability=%s | operation=%s",
            fallback_reason,
            str(state.get("task_id") or getattr(runtime_context, "task_internal_id", "")),
            str(state.get("target_capability") or ""),
            str(state.get("operation") or ""),
        )
        return self.plan(state)

    def plan(self, state: dict) -> DynamicPlan:
        goal = str(state.get("goal") or state.get("dynamic_goal") or "完成当前任务")
        target = str(state.get("target_capability") or "")
        operation = str(state.get("operation") or "qa")
        attachments = list(state.get("attachment_refs") or [])
        docx_ref = self._first_docx_ref(attachments)

        if target == "document_qa" and operation in {"summarize", "analyze", "extract", "compare"} and docx_ref:
            if operation == "compare" and self._should_include_knowledge_search(state, goal):
                return DynamicPlan(
                    goal=goal,
                    revision=int(state.get("plan_revision") or 1),
                    steps=[
                        DynamicPlanStep(
                            step_id="step_1",
                            title="瑙ｆ瀽Word鏂囨。",
                            action_type="tool",
                            capability_key="word_document_parse",
                            input_refs=[docx_ref],
                            depends_on=[],
                            success_criteria=[
                                "document_text_non_empty",
                                "document_structure_non_empty",
                            ],
                        ),
                        DynamicPlanStep(
                            step_id="step_2",
                            title="妫€绱㈢煡璇嗗簱",
                            action_type="tool",
                            capability_key="knowledge_search",
                            input_refs=[],
                            depends_on=[],
                            success_criteria=["knowledge_result_available"],
                        ),
                        DynamicPlanStep(
                            step_id="step_3",
                            title="鍒嗘瀽鏂囨。璇佹嵁",
                            action_type="analysis",
                            capability_key="evidence_analysis",
                            input_refs=["step:step_1.output", "step:step_2.output"],
                            depends_on=["step_1", "step_2"],
                            success_criteria=["analysis_addresses_user_goal"],
                        ),
                    ],
                )

            return DynamicPlan(
                goal=goal,
                revision=int(state.get("plan_revision") or 1),
                steps=[
                    DynamicPlanStep(
                        step_id="step_1",
                        title="解析Word文档",
                        action_type="tool",
                        capability_key="word_document_parse",
                        input_refs=[docx_ref],
                        depends_on=[],
                        success_criteria=[
                            "document_text_non_empty",
                            "document_structure_non_empty",
                        ],
                    ),
                    DynamicPlanStep(
                        step_id="step_2",
                        title="分析文档证据",
                        action_type="analysis",
                        capability_key="evidence_analysis",
                        input_refs=["step:step_1.output"],
                        depends_on=["step_1"],
                        success_criteria=["analysis_addresses_user_goal"],
                    ),
                ],
            )

        return DynamicPlan(
            goal=goal,
            revision=int(state.get("plan_revision") or 1),
            steps=[
                DynamicPlanStep(
                    step_id="step_1",
                    title="分析已知证据",
                    action_type="analysis",
                    capability_key="evidence_analysis",
                    input_refs=[],
                    depends_on=[],
                    success_criteria=["analysis_addresses_user_goal"],
                )
            ],
        )

    @staticmethod
    def _first_docx_ref(attachments: list) -> str | None:
        for idx, item in enumerate(attachments):
            if isinstance(item, str) and item.strip():
                return f"attachment:{idx}"
            if not isinstance(item, dict):
                continue
            if _normalized_file_ext(item) == ".docx":
                return str(item.get("ref") or f"attachment:{idx}")
        return None

    @staticmethod
    def _should_include_knowledge_search(state: dict, goal: str) -> bool:
        retrieval_plan = state.get("retrieval_plan_snapshot")
        retrieval_plan = retrieval_plan if isinstance(retrieval_plan, dict) else {}
        maas = str(retrieval_plan.get("maas") or "").lower()
        if maas == "off":
            return False
        if maas in {"required", "auto"}:
            return True

        knowledge_mode = str(state.get("knowledge_mode_snapshot") or "").upper()
        if knowledge_mode in {"MAAS_STRICT", "KNOWLEDGE_REQUIRED"}:
            return True

        keywords = (
            "standard",
            "standards",
            "policy",
            "policies",
            "spec",
            "specification",
            "company",
            "标准",
            "规范",
            "制度",
            "公司",
        )
        return any(keyword in goal for keyword in keywords)

    def _planner_state_ref(self, state: dict, *, attempt: int = 1) -> dict[str, Any]:
        return {
            "goal": state.get("goal") or state.get("dynamic_goal") or "",
            "target_capability": state.get("target_capability") or "",
            "operation": state.get("operation") or "",
            "planner_attempt": attempt,
            "planner_retry_instruction": (
                "Return only strict JSON matching the dynamic_plan_json contract."
                if attempt > 1
                else ""
            ),
            "attachments": list(state.get("attachment_refs") or []),
            "request_understanding_snapshot": state.get("request_understanding_snapshot") or {},
            "retrieval_plan_snapshot": state.get("retrieval_plan_snapshot") or {},
            "knowledge_mode_snapshot": state.get("knowledge_mode_snapshot") or "",
            "runtime_budget": {
                "plan_revision": int(state.get("plan_revision") or 1),
                "replan_count": int(state.get("replan_count") or 0),
                "tool_call_count": int(state.get("tool_call_count") or 0),
            },
            "available_capabilities": [
                {
                    "capability_key": spec.capability_key,
                    "display_name": spec.display_name,
                    "description": spec.description_for_planner,
                    "executor_kind": spec.executor_kind,
                    "input_schema": spec.input_schema,
                    "output_schema": spec.output_schema,
                    "preconditions": list(spec.preconditions),
                    "side_effect": spec.side_effect.value,
                    "risk_level": spec.risk_level,
                    "timeout_seconds": spec.timeout_seconds,
                    "retry_policy": dict(spec.retry_policy),
                    "supported_media_types": list(spec.supported_media_types),
                }
                for spec in self._registry.planner_visible()
            ],
        }


def _planner_user_content(state: dict, *, attempt: int) -> str:
    goal = str(state.get("goal") or state.get("dynamic_goal") or "")
    if attempt <= 1:
        return goal
    return (
        f"{goal}\n\n"
        "Previous planner output could not be parsed as the required strict JSON. "
        "Retry by returning only a valid dynamic_plan_json object. Do not include markdown, prose, or code fences."
    )


def _is_retryable_planner_contract_error(exc: Exception) -> bool:
    return isinstance(exc, (LLMProfileParseError, ValidationError))


def _normalized_file_ext(item: dict[str, Any]) -> str:
    ext = str(item.get("file_ext") or "").strip().lower()
    if ext:
        return ext if ext.startswith(".") else f".{ext}"
    name = str(
        item.get("file_name") or item.get("filename") or item.get("original_name") or ""
    ).strip()
    suffix = Path(name).suffix.lower()
    if suffix:
        return suffix
    mime = str(item.get("mime_type") or "").strip().lower()
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return ".docx"
    return ""


# module-level note (auto-appended):
# DynamicPlanner — 计划生成。
# 关键约束: Phase-1 是 skeleton,Phase 2.8D 后接 real LLM planner。
