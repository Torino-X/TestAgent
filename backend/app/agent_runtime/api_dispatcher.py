"""Fail-closed production dispatcher for LangGraph Agent Tasks.

The retired Legacy Agent Orchestrator is deliberately not injectable here.  Historical
Legacy rows remain readable through repositories/API DTOs, but any attempt to execute
one is rejected with an explicit migration-required error.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol, Set, runtime_checkable

from app.core.exceptions import (
    LangGraphNotReadyError,
    LangGraphRuntimeUnavailableError,
    UnsupportedLegacyTaskError,
)

from .dispatch_errors import (
    IncrementalPayloadInvalidError,
    ParallelDispatchGuardError,
    RepairPayloadInvalidError,
)
from .feature_flags import (
    AgentRuntimeFeatureFlags,
    EngineType,
    LangGraphDisabledError,
    get_feature_flags,
)
from .persistence import ProbeReport
from .persistence.redis_inflight_registry import InflightOwner, RedisInFlightRegistry

logger = logging.getLogger(__name__)

UNSUPPORTED_LEGACY_TASK = "UNSUPPORTED_LEGACY_TASK"


@runtime_checkable
class LangGraphCoordinatorProtocol(Protocol):
    async def run_pre_confirm(self, payload: Any) -> Any: ...

    async def run_post_confirm(self, payload: Any) -> Any: ...

    async def resume_section_confirmation(self, payload: Any) -> Any: ...

    async def resume_preparation_clarification(self, payload: Any) -> Any: ...

    async def resume_format_loss_interrupt(self, payload: Any) -> Any: ...

    async def run_incremental(self, payload: Any) -> Any: ...

    async def run_repair(self, payload: Any) -> Any: ...


class InFlightTaskRegistry:
    """Process-local duplicate-dispatch guard."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: Set[str] = set()

    def begin(self, task_public_id: str, engine: EngineType) -> None:
        with self._lock:
            if task_public_id in self._inflight:
                raise ParallelDispatchGuardError(
                    task_public_id=task_public_id,
                    running_engine="<in-flight>",
                    requested_engine=engine,
                )
            self._inflight.add(task_public_id)

    def end(self, task_public_id: str) -> None:
        with self._lock:
            self._inflight.discard(task_public_id)

    def is_inflight(self, task_public_id: str) -> bool:
        with self._lock:
            return task_public_id in self._inflight

    def __len__(self) -> int:
        with self._lock:
            return len(self._inflight)


@dataclass(frozen=True)
class DispatchOutcome:
    engine: EngineType
    fallback_used: bool = False
    fallback_reason: Optional[str] = None
    result: Any = None
    task_public_id: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class ApiDispatcher:
    """Single production dispatch boundary; only LangGraph is executable."""

    def __init__(
        self,
        *,
        coordinator: LangGraphCoordinatorProtocol | None,
        probe_report: ProbeReport,
        feature_flags: Optional[AgentRuntimeFeatureFlags] = None,
        inflight: Optional[InFlightTaskRegistry] = None,
        redis_inflight: Optional[RedisInFlightRegistry] = None,
    ) -> None:
        self._coordinator = coordinator
        self._probe_report = probe_report
        self._flags = feature_flags or get_feature_flags()
        self._inflight = inflight if inflight is not None else InFlightTaskRegistry()
        self._redis_inflight = redis_inflight

    def _production_langgraph_unlocked(self) -> tuple[bool, str]:
        if self._coordinator is None:
            return False, "coordinator_unavailable"
        if not self._flags.production_dispatch_enabled:
            return False, "env_disabled"
        if self._probe_report.production_dispatch_forced_off:
            return False, "postgres_unhealthy"
        if not self._flags.langgraph_enabled:
            return False, "langgraph_global_disabled"
        if getattr(self._probe_report, "langgraph_readiness", None) is False:
            return False, "langgraph_not_ready"
        return True, ""

    def _resolve_engine(
        self, *, task_public_id: str, task_engine_type: Optional[str]
    ) -> tuple[EngineType, bool, Optional[str]]:
        requested = str(task_engine_type or "").strip().lower()
        if requested != "langgraph":
            logger.error(
                "%s | migration-required | task_id=%s | engine_type=%s",
                UNSUPPORTED_LEGACY_TASK,
                task_public_id,
                requested or "missing",
            )
            raise UnsupportedLegacyTaskError(task_public_id)
        unlocked, reason = self._production_langgraph_unlocked()
        if not unlocked:
            if reason == "coordinator_unavailable":
                raise LangGraphRuntimeUnavailableError(
                    detail={"task_public_id": task_public_id, "reason": reason}
                )
            raise LangGraphNotReadyError(
                reason=reason,
                detail={
                    "task_public_id": task_public_id,
                    "production_dispatch_enabled": self._flags.production_dispatch_enabled,
                    "probe_postgres_ok": self._probe_report.postgres_ok,
                    "probe_production_dispatch_forced_off": (
                        self._probe_report.production_dispatch_forced_off
                    ),
                },
            )
        return "langgraph", False, None

    async def _release_redis_owner(
        self, task_public_id: str, redis_owner: Optional[InflightOwner]
    ) -> None:
        if self._redis_inflight is None or redis_owner is None:
            return
        try:
            await self._redis_inflight.release(task_public_id, redis_owner)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ApiDispatcher Redis in-flight release failed; TTL will reclaim "
                "task=%s err=%s",
                task_public_id,
                exc,
            )

    async def _run_guarded(
        self,
        *,
        task_public_id: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> DispatchOutcome:
        engine: EngineType = "langgraph"
        redis_owner: Optional[InflightOwner] = None
        if self._redis_inflight is not None:
            redis_owner = await self._redis_inflight.try_acquire(task_public_id, engine)
        self._inflight.begin(task_public_id, engine)
        try:
            result = await operation()
            return DispatchOutcome(
                engine=engine,
                result=result,
                task_public_id=task_public_id,
            )
        finally:
            self._inflight.end(task_public_id)
            await self._release_redis_owner(task_public_id, redis_owner)

    async def dispatch_new_task(
        self,
        *,
        task_public_id: str,
        task_engine_type: Optional[str],
        context: Any,
    ) -> DispatchOutcome:
        self._resolve_engine(
            task_public_id=task_public_id,
            task_engine_type=task_engine_type,
        )
        return await self._run_guarded(
            task_public_id=task_public_id,
            operation=lambda: self._require_coordinator().run_pre_confirm(context),
        )

    async def dispatch_resume(
        self,
        *,
        task_public_id: str,
        task_engine_type: Optional[str],
        context: Any,
    ) -> DispatchOutcome:
        self._resolve_engine(
            task_public_id=task_public_id,
            task_engine_type=task_engine_type,
        )
        payload = self._section_resume_payload(task_public_id, context)
        return await self._run_guarded(
            task_public_id=task_public_id,
            operation=lambda: self._require_coordinator().resume_section_confirmation(payload),
        )

    async def dispatch_confirm(
        self,
        *,
        task_public_id: str,
        task_engine_type: Optional[str],
        context: Any,
    ) -> DispatchOutcome:
        self._resolve_engine(
            task_public_id=task_public_id,
            task_engine_type=task_engine_type,
        )
        return await self._run_guarded(
            task_public_id=task_public_id,
            operation=lambda: self._require_coordinator().run_post_confirm(context),
        )

    async def dispatch_format_loss_decision(
        self,
        *,
        task_public_id: str,
        task_engine_type: Optional[str],
        payload: Dict[str, Any],
    ) -> DispatchOutcome:
        self._resolve_engine(
            task_public_id=task_public_id,
            task_engine_type=task_engine_type,
        )
        return await self._run_guarded(
            task_public_id=task_public_id,
            operation=lambda: self._require_coordinator().resume_format_loss_interrupt(payload),
        )

    async def dispatch_preparation_clarification(
        self,
        *,
        task_public_id: str,
        task_engine_type: Optional[str],
        payload: Dict[str, Any],
    ) -> DispatchOutcome:
        self._resolve_engine(
            task_public_id=task_public_id,
            task_engine_type=task_engine_type,
        )
        return await self._run_guarded(
            task_public_id=task_public_id,
            operation=lambda: self._require_coordinator().resume_preparation_clarification(payload),
        )

    async def dispatch_incremental_task(
        self,
        *,
        task_public_id: str,
        task_engine_type: Optional[str],
        payload: Dict[str, Any],
    ) -> DispatchOutcome:
        self._validate_incremental_payload(
            task_public_id=task_public_id,
            payload=payload,
            kind="incremental_task",
        )
        self._resolve_engine(
            task_public_id=task_public_id,
            task_engine_type=task_engine_type,
        )
        return await self._run_guarded(
            task_public_id=task_public_id,
            operation=lambda: self._require_coordinator().run_incremental(payload),
        )

    async def dispatch_incremental_resume(
        self,
        *,
        task_public_id: str,
        task_engine_type: Optional[str],
        payload: Dict[str, Any],
    ) -> DispatchOutcome:
        self._validate_incremental_payload(
            task_public_id=task_public_id,
            payload=payload,
            kind="incremental_resume",
        )
        self._resolve_engine(
            task_public_id=task_public_id,
            task_engine_type=task_engine_type,
        )
        return await self._run_guarded(
            task_public_id=task_public_id,
            operation=lambda: self._require_coordinator().run_incremental(payload),
        )

    async def dispatch_repair_task(
        self,
        *,
        task_public_id: str,
        task_engine_type: Optional[str],
        payload: Dict[str, Any],
    ) -> DispatchOutcome:
        self._validate_repair_payload(task_public_id=task_public_id, payload=payload)
        self._resolve_engine(
            task_public_id=task_public_id,
            task_engine_type=task_engine_type,
        )
        return await self._run_guarded(
            task_public_id=task_public_id,
            operation=lambda: self._require_coordinator().run_repair(payload),
        )

    @staticmethod
    def _section_resume_payload(task_public_id: str, context: Any) -> Any:
        if not isinstance(context, dict):
            return {
                "task_id": getattr(context, "task_id", task_public_id),
                "graph_run_id": getattr(context, "graph_run_id", None)
                or f"run-{getattr(context, 'task_internal_id', task_public_id)}",
                "decision": {
                    "kind": "section_confirmation",
                    "sections": getattr(context, "section_confirm_config", {}).get(
                        "sections", []
                    ),
                    "source": "user",
                },
            }
        payload_json = context.get("payload") or {}
        sections = context.get("sections") or (
            payload_json.get("sections", []) if isinstance(payload_json, dict) else []
        )
        return {
            "task_id": context.get("task_public_id") or task_public_id,
            "graph_run_id": context.get("graph_run_id")
            or f"run-{context.get('task_internal_id', task_public_id)}",
            "decision": {
                "kind": "section_confirmation",
                "sections": sections,
                "source": context.get("source") or "user",
            },
        }

    @staticmethod
    def _validate_incremental_payload(
        *, task_public_id: str, payload: Dict[str, Any], kind: str
    ) -> None:
        if not isinstance(payload, dict):
            raise IncrementalPayloadInvalidError(
                task_public_id=task_public_id,
                missing_field="payload_dict",
                detail=f"kind={kind} expects dict payload",
            )
        if kind == "incremental_task":
            for name in (
                "incremental_intent",
                "source_artifact_public_id",
                "modification_idempotency_key",
            ):
                if not payload.get(name):
                    raise IncrementalPayloadInvalidError(
                        task_public_id=task_public_id,
                        missing_field=name,
                        detail=f"kind={kind} requires {name!r}",
                    )
        elif payload.get("kind") != "incremental_resume":
            raise IncrementalPayloadInvalidError(
                task_public_id=task_public_id,
                missing_field="kind=incremental_resume",
                detail="incremental resume payload kind is invalid",
            )
        elif not isinstance(payload.get("decision"), dict):
            raise IncrementalPayloadInvalidError(
                task_public_id=task_public_id,
                missing_field="decision",
                detail="incremental resume requires a decision object",
            )

    @staticmethod
    def _validate_repair_payload(
        *, task_public_id: str, payload: Dict[str, Any]
    ) -> None:
        if not isinstance(payload, dict) or not payload.get("task_id"):
            raise RepairPayloadInvalidError(
                task_public_id=task_public_id,
                missing_field="task_id",
                detail="repair payload requires task_id",
            )

    def _require_coordinator(self) -> LangGraphCoordinatorProtocol:
        if self._coordinator is None:
            raise LangGraphRuntimeUnavailableError(
                detail={"reason": "coordinator_unavailable"}
            )
        return self._coordinator

    async def dispatch_from_outbox_row(
        self,
        *,
        row: Any,
        session: Any,
    ) -> DispatchOutcome:
        engine = str(getattr(row, "engine_type", "") or "").strip().lower()
        task_hint = str(getattr(row, "public_id", "") or "")
        self._resolve_engine(
            task_public_id=task_hint,
            task_engine_type=engine,
        )

        from app.agent_runtime.incremental.subgraph import GRAPH_NAME_INCREMENTAL
        from app.services.agent_context_factory import (
            build_agent_context_from_task_internal_id,
        )

        ctx = await build_agent_context_from_task_internal_id(
            session=session,
            task_internal_id=int(row.task_id),
        )
        payload_json = row.payload_json if isinstance(row.payload_json, dict) else {}
        task_context = (
            ctx.task_context_json if isinstance(ctx.task_context_json, dict) else {}
        )
        request_understanding = task_context.get("request_understanding_snapshot")
        request_understanding = (
            request_understanding if isinstance(request_understanding, dict) else {}
        )
        payload_understanding = payload_json.get("request_understanding_snapshot")
        payload_understanding = (
            payload_understanding if isinstance(payload_understanding, dict) else {}
        )
        graph_run_id = payload_json.get("graph_run_id")
        if not isinstance(graph_run_id, str):
            graph_run_id = f"run-{ctx.task_internal_id}"
        initial_payload = {
            **task_context,
            **payload_json,
            "task_id": ctx.task_id,
            "task_internal_id": ctx.task_internal_id,
            "conversation_internal_id": ctx.conversation_internal_id,
            "conversation_public_id": ctx.conversation_id,
            "user_internal_id": ctx.user_internal_id,
            "user_id": ctx.user_id,
            "user_prompt": getattr(ctx, "user_prompt", None),
            "requirement_file_id": ctx.requirement_file_id,
            "template_file_id": ctx.template_file_id,
            "intent": payload_json.get("intent") or task_context.get("intent"),
            "route": payload_json.get("route") or task_context.get("route"),
            "user_instruction": payload_json.get("user_instruction")
            or task_context.get("user_goal")
            or getattr(ctx, "user_prompt", None),
            "goal": payload_json.get("goal")
            or payload_json.get("dynamic_goal")
            or task_context.get("dynamic_goal")
            or task_context.get("user_goal")
            or getattr(ctx, "user_prompt", None),
            "dynamic_goal": payload_json.get("dynamic_goal")
            or task_context.get("dynamic_goal")
            or task_context.get("user_goal")
            or getattr(ctx, "user_prompt", None),
            "target_capability": payload_json.get("target_capability")
            or task_context.get("target_capability")
            or payload_understanding.get("target_capability")
            or request_understanding.get("target_capability"),
            "operation": payload_json.get("operation")
            or task_context.get("operation")
            or payload_understanding.get("operation")
            or request_understanding.get("operation"),
            "attachment_refs": payload_json.get("attachment_refs")
            or task_context.get("attachment_refs")
            or [],
            "request_understanding_snapshot": payload_json.get(
                "request_understanding_snapshot"
            )
            or task_context.get("request_understanding_snapshot")
            or {},
            "capability_routing_snapshot": payload_json.get(
                "capability_routing_snapshot"
            )
            or task_context.get("capability_routing_snapshot")
            or {},
            "graph_name": getattr(row, "graph_name", None)
            or payload_json.get("graph_name"),
            "graph_version": getattr(row, "graph_version", None)
            or payload_json.get("graph_version"),
            "graph_run_id": graph_run_id,
        }
        selected_graph = str(initial_payload.get("graph_name") or "").strip()
        if (
            selected_graph == GRAPH_NAME_INCREMENTAL
            or initial_payload.get("request_type") == "incremental_task"
            or initial_payload.get("execution_mode") == "incremental_agent"
        ):
            self._validate_incremental_payload(
                task_public_id=ctx.task_id,
                payload=initial_payload,
                kind="incremental_task",
            )
            operation = lambda: self._require_coordinator().run_incremental(
                initial_payload
            )
        else:
            operation = lambda: self._require_coordinator().run_pre_confirm(
                initial_payload
            )
        return await self._run_guarded(
            task_public_id=ctx.task_id,
            operation=operation,
        )

    def complete_task(self, task_public_id: str) -> None:
        self._inflight.end(task_public_id)

    async def aclose(self) -> None:
        if self._redis_inflight is not None:
            await self._redis_inflight.aclose()

    @property
    def inflight(self) -> InFlightTaskRegistry:
        return self._inflight

    @property
    def redis_inflight(self) -> Optional[RedisInFlightRegistry]:
        return self._redis_inflight

    @property
    def probe_report(self) -> ProbeReport:
        return self._probe_report

    @property
    def feature_flags(self) -> AgentRuntimeFeatureFlags:
        return self._flags


def ensure_langgraph_dispatchable(
    *, task_engine_type: Optional[str], feature_flags: AgentRuntimeFeatureFlags
) -> None:
    if str(task_engine_type or "").strip().lower() != "langgraph":
        raise UnsupportedLegacyTaskError()
    if not feature_flags.langgraph_enabled:
        raise LangGraphDisabledError()


__all__ = [
    "ApiDispatcher",
    "DispatchOutcome",
    "InFlightTaskRegistry",
    "LangGraphCoordinatorProtocol",
]
