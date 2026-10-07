"""LangGraph-only ApiDispatcher contracts after Legacy retirement."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from app.agent_runtime.api_dispatcher import ApiDispatcher, InFlightTaskRegistry
from app.agent_runtime.feature_flags import get_feature_flags
from app.core.exceptions import (
    LangGraphNotReadyError,
    LangGraphRuntimeUnavailableError,
    UnsupportedLegacyTaskError,
)


class SpyCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def run_pre_confirm(self, payload):
        self.calls.append(("pre", payload))
        return {"ok": True}

    async def run_post_confirm(self, payload):
        self.calls.append(("post", payload))
        return {"ok": True}

    async def resume_section_confirmation(self, payload):
        self.calls.append(("resume", payload))
        return {"ok": True}

    async def resume_format_loss_interrupt(self, payload):
        self.calls.append(("format", payload))
        return {"ok": True}

    async def run_incremental(self, payload):
        self.calls.append(("incremental", payload))
        return {"ok": True}

    async def run_repair(self, payload):
        self.calls.append(("repair", payload))
        return {"ok": True}


def _dispatcher(*, ready: bool = True, coordinator=None) -> ApiDispatcher:
    probe = SimpleNamespace(
        postgres_ok=ready,
        production_dispatch_forced_off=not ready,
        langgraph_readiness=ready,
    )
    flags = dataclasses.replace(
        get_feature_flags(),
        langgraph_enabled=True,
        production_dispatch_enabled=True,
    )
    return ApiDispatcher(
        coordinator=coordinator if coordinator is not None else SpyCoordinator(),
        probe_report=probe,
        feature_flags=flags,
    )


def test_historical_legacy_and_missing_engine_are_rejected() -> None:
    dispatcher = _dispatcher()
    for engine in ("legacy", None, ""):
        with pytest.raises(UnsupportedLegacyTaskError) as captured:
            dispatcher._resolve_engine(
                task_public_id="task_old",
                task_engine_type=engine,
            )
        assert captured.value.detail["migration_status"] == "migration-required"


def test_unhealthy_langgraph_fails_closed() -> None:
    with pytest.raises(LangGraphNotReadyError):
        _dispatcher(ready=False)._resolve_engine(
            task_public_id="task_1",
            task_engine_type="langgraph",
        )


def test_missing_coordinator_is_explicitly_unavailable() -> None:
    dispatcher = _dispatcher(coordinator=SpyCoordinator())
    dispatcher._coordinator = None
    with pytest.raises(LangGraphRuntimeUnavailableError):
        dispatcher._resolve_engine(
            task_public_id="task_1",
            task_engine_type="langgraph",
        )


def test_inflight_registry_lifecycle_and_duplicate_guard() -> None:
    registry = InFlightTaskRegistry()
    registry.begin("task_1", "langgraph")
    assert registry.is_inflight("task_1")
    with pytest.raises(Exception):
        registry.begin("task_1", "langgraph")
    registry.end("task_1")
    assert not registry.is_inflight("task_1")


@pytest.mark.asyncio
async def test_new_and_confirm_dispatch_only_to_langgraph() -> None:
    coordinator = SpyCoordinator()
    dispatcher = _dispatcher(coordinator=coordinator)
    created = await dispatcher.dispatch_new_task(
        task_public_id="task_1",
        task_engine_type="langgraph",
        context={"task_id": "task_1"},
    )
    confirmed = await dispatcher.dispatch_confirm(
        task_public_id="task_1",
        task_engine_type="langgraph",
        context={"task_id": "task_1"},
    )
    assert created.engine == confirmed.engine == "langgraph"
    assert coordinator.calls == [
        ("pre", {"task_id": "task_1"}),
        ("post", {"task_id": "task_1"}),
    ]
