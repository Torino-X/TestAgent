from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.agent_runtime.api_dispatcher import ApiDispatcher
from app.agent_runtime.feature_flags import AgentRuntimeFeatureFlags
from app.agent_runtime.langgraph_dispatch_adapter import LangGraphDispatchAdapter
from app.agent_runtime.persistence.probe_router import ProbeReport


class _NoopOrchestrator:
    async def run(self, ctx: Any) -> Any:
        return ctx

    async def resume_after_confirm(self, ctx: Any) -> Any:
        return ctx


class _SpyCoordinator:
    def __init__(self) -> None:
        self.format_loss_payloads: list[dict[str, Any]] = []
        self.section_payloads: list[dict[str, Any]] = []

    async def run_pre_confirm(self, payload: Any) -> Any:
        return payload

    async def run_post_confirm(self, payload: Any) -> Any:
        return payload

    async def resume_section_confirmation(self, payload: Any) -> Any:
        self.section_payloads.append(payload)
        return {"path": "section"}

    async def resume_format_loss_interrupt(self, payload: Any) -> Any:
        self.format_loss_payloads.append(payload)
        return {"path": "format_loss"}

    async def run_incremental(self, payload: Any) -> Any:
        return payload

    async def run_repair(self, payload: Any) -> Any:
        return payload


def _probe_report() -> ProbeReport:
    return ProbeReport(
        postgres_ok=True,
        postgres_url_echo="postgresql://test:***@localhost/db",
        postgres_latency_ms=1,
        eventbus_kind="InMemory",
        eventbus_url_echo="",
        redis_ok=True,
        ready=True,
        production_dispatch_forced_off=False,
        langgraph_readiness=True,
    )


def _flags() -> AgentRuntimeFeatureFlags:
    return AgentRuntimeFeatureFlags(
        langgraph_enabled=True,
        production_dispatch_enabled=True,
    )


def test_format_loss_decision_api_uses_numeric_error_codes() -> None:
    """API response.error only accepts int codes; this route must not pass strings."""
    from app.api.v1 import agent_tasks

    source = inspect.getsource(agent_tasks.submit_format_loss_decision)
    forbidden = [
        'code="INVALID_DECISION"',
        'code="TASK_NOT_FOUND"',
        'code="FORBIDDEN"',
        'code="SCOPE_INTEGRITY_ERROR"',
        'code="TASK_NOT_AWAITING_DECISION"',
        'code="CONTEXT_RESTORE_FAILED"',
        'code="NO_PENDING_LOSSES"',
    ]
    for fragment in forbidden:
        assert fragment not in source


def test_format_loss_decision_rejects_historical_engine_before_dispatch() -> None:
    """A historical engine is rejected before any LangGraph resume call."""
    from app.api.v1 import agent_tasks

    source = inspect.getsource(agent_tasks.submit_format_loss_decision)
    engine_guard_idx = source.index('task_engine_type != "langgraph"')
    dispatch_idx = source.index("dispatch_format_loss_decision")

    assert engine_guard_idx < dispatch_idx
    assert "MIGRATION_REQUIRED" in source[engine_guard_idx:dispatch_idx]


@pytest.mark.asyncio
async def test_dispatch_format_loss_decision_calls_format_loss_resume() -> None:
    coordinator = _SpyCoordinator()
    dispatcher = ApiDispatcher(
        coordinator=coordinator,
        probe_report=_probe_report(),
        feature_flags=_flags(),
    )

    outcome = await dispatcher.dispatch_format_loss_decision(
        task_public_id="task_v3",
        task_engine_type="langgraph",
        payload={
            "task_id": "task_v3",
            "decision": {
                "kind": "format_loss",
                "decision": "accept",
                "source": "user",
            },
        },
    )

    assert outcome.engine == "langgraph"
    assert outcome.result == {"path": "format_loss"}
    assert coordinator.format_loss_payloads
    assert coordinator.section_payloads == []


@pytest.mark.asyncio
async def test_langgraph_adapter_maps_format_loss_payload_to_coordinator_method() -> None:
    class Coordinator:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def resume_format_loss_interrupt(
            self,
            *,
            task_id: str,
            graph_run_id: str,
            decision: dict[str, Any],
        ) -> Any:
            self.calls.append({
                "task_id": task_id,
                "graph_run_id": graph_run_id,
                "decision": decision,
            })
            return {"ok": True}

    coordinator = Coordinator()
    adapter = LangGraphDispatchAdapter(coordinator)

    result = await adapter.resume_format_loss_interrupt({
        "task_id": "task_v3",
        "graph_run_id": "run-task_v3",
        "decision": {
            "kind": "format_loss",
            "decision": "accept",
            "source": "user",
        },
    })

    assert result == {"ok": True}
    assert coordinator.calls == [
        {
            "task_id": "task_v3",
            "graph_run_id": "run-task_v3",
            "decision": {
                "kind": "format_loss",
                "decision": "accept",
                "source": "user",
            },
        }
    ]
