"""Dynamic step executor."""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable

from app.agent.atomic_capability_registry import AtomicCapabilityRegistry
from app.context_engine.models.context import ContextRequest
from app.llm.task_profiles import (
    LLMParseFailurePolicy,
    LLMParserType,
    LLMTaskProfile,
)
from app.utils.ids import generate_public_id

from .schemas import DynamicObservation, DynamicPlanStep
from .tool_handlers import FileResolver, KnowledgeSearchHandler, WordDocumentParseHandler

logger = logging.getLogger(__name__)

StepHandler = Callable[[DynamicPlanStep, dict], Awaitable[dict[str, Any]] | dict[str, Any]]

DYNAMIC_AGENT_EVIDENCE_ANALYSIS_PROFILE = LLMTaskProfile(
    name="dynamic_agent_evidence_analysis",
    system_prompt=(
        "Analyze the verified Dynamic Agent evidence and answer the user's goal. "
        "Use only provided evidence. If required knowledge search has no hits, say so explicitly."
    ),
    parser=LLMParserType.MARKDOWN,
    allow_markdown=True,
    require_json=False,
    max_tokens=2000,
    temperature=0.2,
    on_parse_failure=LLMParseFailurePolicy.RAISE,
)

_MAX_EVIDENCE_TEXT_CHARS = 24000
_MAX_EVIDENCE_ITEM_CHARS = 6000


class DynamicStepExecutor:
    def __init__(
        self,
        registry: AtomicCapabilityRegistry,
        *,
        handlers: dict[str, StepHandler] | None = None,
        runtime_context: Any = None,
        file_resolver: FileResolver | None = None,
    ) -> None:
        self._registry = registry
        self._handlers = dict(handlers or {})
        self._runtime_context = runtime_context
        self._file_resolver = file_resolver

    async def execute(self, step: DynamicPlanStep, state: dict) -> DynamicObservation:
        spec = self._registry.require(step.capability_key)
        handler = self._handlers.get(step.capability_key)
        if handler is None and step.capability_key == "evidence_analysis":
            result = self._run_evidence_analysis(step, state)
            if inspect.isawaitable(result):
                result = await result
        elif handler is None and step.capability_key == "word_document_parse":
            result = await self._run_word_document_parse(step, state)
        elif handler is None and step.capability_key == "knowledge_search":
            result = await self._run_knowledge_search(step, state)
        elif handler is None:
            logger.warning(
                "FALLBACK_USED | component=dynamic_agent.executor | "
                "from=capability_handler | to=not_configured_result | "
                "reason=handler_missing | task_id=%s | capability=%s | step_id=%s",
                str(state.get("task_id") or ""),
                step.capability_key,
                step.step_id,
            )
            result = _not_configured(step.capability_key)
        else:
            result = handler(step, state)
            if inspect.isawaitable(result):
                result = await result

        success = bool(result.get("success"))
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return DynamicObservation(
            observation_id=generate_public_id("obs"),
            step_id=step.step_id,
            capability_key=step.capability_key,
            status="success" if success else "failed",
            summary=str(result.get("summary") or spec.display_name),
            result_ref=f"state:tool_results.{step.step_id}",
            facts=self._facts_for(
                step.capability_key,
                data,
                error=result.get("error"),
            ),
            data=data,
            warnings=list(result.get("warnings") or []),
        )

    async def _run_word_document_parse(
        self,
        step: DynamicPlanStep,
        state: dict,
    ) -> dict[str, Any]:
        if self._runtime_context is None:
            logger.warning(
                "FALLBACK_USED | component=dynamic_agent.executor | "
                "from=runtime_tool_adapter | to=not_configured_result | "
                "reason=runtime_context_missing | task_id=%s | capability=%s | step_id=%s",
                str(state.get("task_id") or ""),
                step.capability_key,
                step.step_id,
            )
            return _not_configured(step.capability_key)
        result = WordDocumentParseHandler(
            ctx_runtime=self._runtime_context,
            file_resolver=self._file_resolver,
        )(step, state)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def _run_knowledge_search(
        self,
        step: DynamicPlanStep,
        state: dict,
    ) -> dict[str, Any]:
        if self._runtime_context is None:
            logger.warning(
                "FALLBACK_USED | component=dynamic_agent.executor | "
                "from=runtime_tool_adapter | to=not_configured_result | "
                "reason=runtime_context_missing | task_id=%s | capability=%s | step_id=%s",
                str(state.get("task_id") or ""),
                step.capability_key,
                step.step_id,
            )
            return _not_configured(step.capability_key)
        result = KnowledgeSearchHandler(ctx_runtime=self._runtime_context)(step, state)
        if inspect.isawaitable(result):
            result = await result
        return result

    def _run_evidence_analysis(
        self,
        step: DynamicPlanStep,
        state: dict,
    ) -> Awaitable[dict[str, Any]] | dict[str, Any]:
        fallback_reason = "runtime_context_missing"
        if self._runtime_context is not None:
            from app.context_engine.feature_flags import is_agent_context_migration_enabled

            resolver = getattr(self._runtime_context, "task_flag_resolver", None)
            try:
                narrative_enabled = is_agent_context_migration_enabled(
                    resolver, "MIG_NARRATIVE"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Dynamic Agent task flag resolution failed closed: %s",
                    exc,
                )
                narrative_enabled = False
            if not narrative_enabled:
                return self._deterministic_evidence_analysis(
                    step,
                    state,
                    "task_context_engine_disabled",
                )
            invoker = getattr(self._runtime_context, "context_llm_invoker", None)
            if invoker is not None:
                if hasattr(invoker, "generate") and getattr(invoker, "available", True):
                    return self._run_evidence_analysis_bridge(step, state, invoker)
                if hasattr(invoker, "invoke"):
                    return self._run_evidence_analysis_invoker(step, state, invoker)
                fallback_reason = "context_llm_invoker_unavailable"
            else:
                fallback_reason = "context_llm_invoker_missing"

        return self._deterministic_evidence_analysis(step, state, fallback_reason)

    async def _run_evidence_analysis_bridge(
        self,
        step: DynamicPlanStep,
        state: dict,
        bridge: Any,
    ) -> dict[str, Any]:
        try:
            bres = await bridge.generate(
                user_id=getattr(self._runtime_context, "user_internal_id", 0),
                call_site="ce.pilot.summarize",
                llm_task_profile=DYNAMIC_AGENT_EVIDENCE_ANALYSIS_PROFILE,
                current_node="evidence_analysis",
                current_goal=str(state.get("goal") or state.get("dynamic_goal") or ""),
                task_state_ref=_evidence_state_ref(step, state),
                output_contract="markdown",
                user_content=str(state.get("goal") or state.get("dynamic_goal") or ""),
                conversation_id=getattr(self._runtime_context, "conversation_internal_id", None),
                task_id=str(state.get("task_id") or getattr(self._runtime_context, "task_internal_id", "")),
                runtime_context=self._runtime_context,
            )
        except Exception as exc:  # noqa: BLE001
            return self._deterministic_evidence_analysis(
                step,
                state,
                f"context_bridge_exception:{type(exc).__name__}",
                err=exc,
            )
        if bres is None or getattr(bres, "value", None) is None:
            return self._deterministic_evidence_analysis(
                step,
                state,
                "context_bridge_empty_response",
            )
        analysis = _analysis_text(getattr(bres, "value", None))
        data = {"analysis": analysis}
        patch = getattr(bres, "context_state_patch", None)
        if isinstance(patch, dict):
            data.update(patch)
        stats = getattr(bres, "stats", None)
        if isinstance(stats, dict):
            data["context_stats"] = dict(stats)
        snapshot_public_id = getattr(bres, "snapshot_public_id", None)
        if snapshot_public_id:
            data["context_snapshot_public_id"] = snapshot_public_id
        return {
            "success": True,
            "summary": analysis,
            "data": data,
        }

    async def _run_evidence_analysis_invoker(
        self,
        step: DynamicPlanStep,
        state: dict,
        invoker: Any,
    ) -> dict[str, Any]:
        request = ContextRequest(
            user_id=str(getattr(self._runtime_context, "user_internal_id", 0)),
            conversation_id=str(getattr(self._runtime_context, "conversation_internal_id", "")),
            task_id=str(state.get("task_id") or getattr(self._runtime_context, "task_internal_id", "")),
            call_site="ce.pilot.summarize",
            current_node="evidence_analysis",
            current_user_message=str(state.get("goal") or state.get("dynamic_goal") or ""),
            state_ref=_evidence_state_ref(step, state),
            thread_id=str(state.get("task_id") or ""),
        )
        try:
            result = await invoker.invoke(
                request=request,
                llm_task_profile=DYNAMIC_AGENT_EVIDENCE_ANALYSIS_PROFILE,
                runtime_context=self._runtime_context,
            )
        except Exception as exc:  # noqa: BLE001
            return self._deterministic_evidence_analysis(
                step,
                state,
                f"context_invoker_exception:{type(exc).__name__}",
                err=exc,
            )
        if result is None or getattr(result, "value", None) is None:
            return self._deterministic_evidence_analysis(
                step,
                state,
                "context_invoker_empty_response",
            )
        analysis = _analysis_text(getattr(result, "value", None))
        data = {"analysis": analysis}
        snapshot_public_id = getattr(result, "snapshot_public_id", None)
        if snapshot_public_id:
            data["context_snapshot_public_id"] = snapshot_public_id
        token_usage = getattr(result, "token_usage", None)
        if isinstance(token_usage, dict):
            data["token_usage"] = dict(token_usage)
        return {
            "success": True,
            "summary": analysis,
            "data": data,
        }

    def _deterministic_evidence_analysis(
        self,
        step: DynamicPlanStep,
        state: dict,
        reason: str,
        *,
        err: Exception | None = None,
    ) -> dict[str, Any]:
        logger.warning(
            "FALLBACK_USED | component=dynamic_agent.executor | "
            "from=context_engine_analysis | to=deterministic_summary | "
            "reason=%s | task_id=%s | capability=%s | step_id=%s | err=%s",
            reason,
            str(state.get("task_id") or getattr(self._runtime_context, "task_internal_id", "")),
            step.capability_key,
            step.step_id,
            str(err)[:300] if err is not None else "",
        )
        observations = list(state.get("observations") or [])
        source = _evidence_source_text(state)
        if not source:
            source = "\n".join(
                str(item.get("summary") or "")
                for item in observations
                if isinstance(item, dict) and item.get("status") == "success"
            ).strip()
        analysis = source or str(
            state.get("goal")
            or state.get("dynamic_goal")
            or "Evidence analysis completed."
        )
        return {
            "success": True,
            "summary": analysis,
            "data": {"analysis": analysis},
        }

    @staticmethod
    def _facts_for(
        capability_key: str,
        data: dict[str, Any],
        *,
        error: Any = None,
    ) -> dict[str, Any]:
        if isinstance(error, dict) and error.get("code"):
            return {"error_code": str(error.get("code"))}
        if capability_key == "word_document_parse":
            text = str(data.get("text_content") or "")
            structure = data.get("document_structure")
            return {
                "text_length": len(text),
                "has_structure": bool(structure),
            }
        if capability_key == "evidence_analysis":
            return {"analysis_length": len(str(data.get("analysis") or ""))}
        if capability_key == "knowledge_search":
            return {"hit_count": int(data.get("hit_count") or 0)}
        return {}


def _not_configured(capability_key: str) -> dict[str, Any]:
    return {
        "success": False,
        "summary": f"capability handler not configured: {capability_key}",
        "data": {},
        "error": {"code": "CAPABILITY_HANDLER_NOT_CONFIGURED"},
    }


def _context_analysis_failed() -> dict[str, Any]:
    return {
        "success": False,
        "summary": "Context Engine evidence analysis failed.",
        "data": {},
        "error": {"code": "CONTEXT_ENGINE_ANALYSIS_FAILED"},
    }


def _analysis_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("analysis") or value.get("summary") or value).strip()
    return str(value or "").strip()


def _evidence_state_ref(step: DynamicPlanStep, state: dict) -> dict[str, Any]:
    return {
        "goal": state.get("goal") or state.get("dynamic_goal") or "",
        "operation": state.get("operation") or "",
        "step_id": step.step_id,
        "input_refs": list(step.input_refs),
        "observations": list(state.get("observations") or []),
        "tool_result_evidence": _tool_result_evidence(state),
        "tool_result_facts": {
            key: {
                "status": value.get("status"),
                "summary": value.get("summary"),
                "facts": value.get("facts"),
            }
            for key, value in dict(state.get("tool_results") or {}).items()
            if isinstance(value, dict)
        },
        "retrieval_plan_snapshot": state.get("retrieval_plan_snapshot") or {},
        "knowledge_mode_snapshot": state.get("knowledge_mode_snapshot") or "",
    }


def _tool_result_evidence(state: dict) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for key, value in dict(state.get("tool_results") or {}).items():
        if not isinstance(value, dict):
            continue
        data = value.get("data") if isinstance(value.get("data"), dict) else {}
        selected_data: dict[str, Any] = {}
        for data_key in (
            "text_content",
            "document_structure",
            "table_summaries",
            "image_texts",
            "analysis",
            "hit_count",
            "results",
        ):
            if data_key in data:
                selected_data[data_key] = _bounded_evidence_value(data[data_key])
        evidence[str(key)] = {
            "status": value.get("status"),
            "summary": _bounded_string(value.get("summary"), _MAX_EVIDENCE_ITEM_CHARS),
            "facts": value.get("facts"),
            "data": selected_data,
        }
    return evidence


def _evidence_source_text(state: dict) -> str:
    chunks: list[str] = []
    for value in dict(state.get("tool_results") or {}).values():
        if not isinstance(value, dict) or value.get("status") != "success":
            continue
        data = value.get("data") if isinstance(value.get("data"), dict) else {}
        for key in ("analysis", "text_content"):
            text = str(data.get(key) or "").strip()
            if text:
                chunks.append(text)
        for key in ("document_structure", "table_summaries", "image_texts"):
            item = data.get(key)
            if item:
                chunks.append(_bounded_string(item, _MAX_EVIDENCE_ITEM_CHARS))
    return _bounded_string("\n\n".join(chunks), _MAX_EVIDENCE_TEXT_CHARS)


def _bounded_evidence_value(value: Any) -> Any:
    if isinstance(value, str):
        return _bounded_string(value, _MAX_EVIDENCE_TEXT_CHARS)
    if isinstance(value, list):
        return [_bounded_evidence_value(item) for item in value[:80]]
    if isinstance(value, dict):
        return {
            str(key): _bounded_evidence_value(item)
            for key, item in list(value.items())[:80]
        }
    return value


def _bounded_string(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]"


# module-level note (auto-appended):
# DynamicStepExecutor — 步骤执行器。
# 关键约束: Phase-1 仅占位(无 LLM),Phase 2.8D 后接 real executor。
