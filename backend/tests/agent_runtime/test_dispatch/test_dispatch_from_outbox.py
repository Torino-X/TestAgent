"""Outbox rows can execute through LangGraph only."""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


def _dispatcher(*, healthy: bool = True):
    coordinator = AsyncMock()
    coordinator.run_pre_confirm.return_value = "pre"
    coordinator.run_incremental.return_value = "incremental"
    flags = dataclasses.replace(
        get_feature_flags(),
        production_dispatch_enabled=True,
        langgraph_enabled=True,
    )
    return ApiDispatcher(
        coordinator=coordinator,
        probe_report=_probe(healthy=healthy),
        feature_flags=flags,
    ), coordinator


def _row(**overrides):
    values = {
        "public_id": "exq-task-new",
        "task_id": 7,
        "engine_type": "langgraph",
        "graph_name": "test_plan_generation",
        "graph_version": "v3",
        "payload_json": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _context(**overrides):
    values = {
        "task_id": "task-new",
        "task_internal_id": 7,
        "conversation_id": "conv-public-8",
        "conversation_internal_id": 8,
        "user_internal_id": 9,
        "user_id": "user-9",
        "user_prompt": "生成测试方案",
        "requirement_file_id": None,
        "template_file_id": None,
        "task_context_json": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", [None, "", "legacy", "repair"])
async def test_outbox_rejects_every_non_langgraph_engine(engine) -> None:
    dispatcher, _ = _dispatcher()
    with pytest.raises(UnsupportedLegacyTaskError):
        await dispatcher.dispatch_from_outbox_row(
            row=_row(engine_type=engine), session=object()
        )


@pytest.mark.asyncio
async def test_outbox_routes_langgraph_and_preserves_context(monkeypatch) -> None:
    async def fake_context(**_kwargs):
        return _context(
            task_context_json={
                "dynamic_goal": "总结文档",
                "target_capability": "document_qa",
                "attachment_refs": ["file-1"],
            }
        )

    monkeypatch.setattr(
        "app.services.agent_context_factory.build_agent_context_from_task_internal_id",
        fake_context,
    )
    dispatcher, coordinator = _dispatcher()
    outcome = await dispatcher.dispatch_from_outbox_row(
        row=_row(payload_json={"graph_run_id": "run-explicit"}),
        session=object(),
    )
    assert outcome.engine == "langgraph"
    assert outcome.task_public_id == "task-new"
    payload = coordinator.run_pre_confirm.await_args.args[0]
    assert payload["graph_run_id"] == "run-explicit"
    assert payload["goal"] == "总结文档"
    assert payload["attachment_refs"] == ["file-1"]


@pytest.mark.asyncio
async def test_outbox_fails_closed_before_context_build_when_unhealthy() -> None:
    dispatcher, _ = _dispatcher(healthy=False)
    with pytest.raises(LangGraphNotReadyError):
        await dispatcher.dispatch_from_outbox_row(row=_row(), session=object())
