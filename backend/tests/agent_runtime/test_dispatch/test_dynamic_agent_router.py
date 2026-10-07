"""LangGraph-only dispatcher routing contract."""
from __future__ import annotations

import dataclasses

import pytest

from app.agent_runtime.api_dispatcher import ApiDispatcher
from app.agent_runtime.feature_flags import get_feature_flags
from app.agent_runtime.persistence import ProbeReport
from app.core.exceptions import LangGraphNotReadyError, UnsupportedLegacyTaskError


def _probe(*, healthy: bool = True) -> ProbeReport:
    return ProbeReport(
        postgres_ok=healthy,
        postgres_url_echo="postgresql://user:***@host/db" if healthy else "",
        postgres_latency_ms=1 if healthy else 0,
        eventbus_kind="InMemory",
        eventbus_url_echo="",
        redis_ok=True,
        ready=healthy,
        production_dispatch_forced_off=not healthy,
        langgraph_readiness=healthy,
        checkpointer_type="AsyncPostgresSaver" if healthy else "none",
    )


class _Coordinator:
    async def run_pre_confirm(self, payload): return payload
    async def run_post_confirm(self, payload): return payload
    async def resume_section_confirmation(self, payload): return payload
    async def resume_format_loss_interrupt(self, payload): return payload
    async def run_incremental(self, payload): return payload
    async def run_repair(self, payload): return payload


def _dispatcher(*, healthy: bool = True, enabled: bool = True) -> ApiDispatcher:
    flags = dataclasses.replace(
        get_feature_flags(),
        production_dispatch_enabled=enabled,
        langgraph_enabled=enabled,
    )
    return ApiDispatcher(
        coordinator=_Coordinator(),
        probe_report=_probe(healthy=healthy),
        feature_flags=flags,
    )


@pytest.mark.parametrize("engine", [None, "", "legacy", "incremental", "mystery"])
def test_non_langgraph_engine_requires_migration(engine) -> None:
    with pytest.raises(UnsupportedLegacyTaskError):
        _dispatcher()._resolve_engine(task_public_id="task-old", task_engine_type=engine)


def test_langgraph_engine_is_the_only_executable_engine() -> None:
    assert _dispatcher()._resolve_engine(
        task_public_id="task-new", task_engine_type="langgraph"
    ) == ("langgraph", False, None)


@pytest.mark.parametrize(
    ("healthy", "enabled"),
    [(False, True), (True, False)],
)
def test_langgraph_readiness_failure_is_fail_closed(healthy, enabled) -> None:
    with pytest.raises(LangGraphNotReadyError):
        _dispatcher(healthy=healthy, enabled=enabled)._resolve_engine(
            task_public_id="task-new", task_engine_type="langgraph"
        )


@pytest.mark.asyncio
async def test_new_task_never_falls_back_when_langgraph_is_unavailable() -> None:
    with pytest.raises(LangGraphNotReadyError):
        await _dispatcher(enabled=False).dispatch_new_task(
            task_public_id="task-new",
            task_engine_type="langgraph",
            context={},
        )
