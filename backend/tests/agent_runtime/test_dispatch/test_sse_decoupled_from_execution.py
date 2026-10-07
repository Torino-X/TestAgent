"""SSE is observation-only; the worker owns all execution."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.agent_tasks import (
    _should_close_pre_confirm_stream,
    _subscribe_task_events,
    task_events_sse,
)


class _SpyDispatcher:
    def __init__(self):
        self.dispatched_new_task = 0

    async def dispatch_new_task(self, *, task_public_id, task_engine_type, context):
        self.dispatched_new_task += 1
        return type("Outcome", (), {"engine": task_engine_type})()


class _FakeRequest:
    """最小的 Request stub,只暴露 app.state。"""

    class _State:
        pass

    def __init__(self, api_dispatcher=None):
        self.app = type("App", (), {})()
        self.app.state = self._State()
        if api_dispatcher is not None:
            self.app.state.api_dispatcher = api_dispatcher


class _FakeLiveEventBus:
    def __init__(self):
        self.subscriptions: list[tuple[str, asyncio.Queue]] = []
        self.unsubscriptions: list[tuple[str, asyncio.Queue]] = []

    async def subscribe(self, *, task_id: str, queue: asyncio.Queue):
        self.subscriptions.append((task_id, queue))
        return queue

    async def unsubscribe(self, *, task_id: str, queue: asyncio.Queue):
        self.unsubscriptions.append((task_id, queue)
        )


class _FakeCurrentUser:
    class _ID:
        pass

    def __init__(self):
        self.id = 1
        self.username = "u"
        self.internal_id = 1


async def _read_source(file_path):
    import pathlib
    return pathlib.Path(file_path).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_sse_does_not_trigger_dispatch_when_worker_attached(monkeypatch):
    """The endpoint must never call a dispatcher, regardless of worker state."""
    from app.api.v1 import agent_tasks as agent_tasks_module
    source = await _read_source(agent_tasks_module.__file__)
    handler_source = source[source.index("async def task_events_sse"):]
    assert "dispatch_new_task(" not in handler_source
    assert "observation-only" in source


@pytest.mark.asyncio
async def test_sse_handler_signature_unchanged(monkeypatch):
    """SSE handler 签名稳定,后向兼容;Stage B 不改 public 路由签名。"""
    import inspect
    sig = inspect.signature(task_events_sse)
    # task_id / current_user / request 应该都在
    params = sig.parameters
    assert "task_id" in params
    assert "current_user" in params


@pytest.mark.asyncio
async def test_sse_has_no_direct_execution_fallback(monkeypatch):
    """There is no pre-retirement direct-dispatch fallback in the API layer."""
    from app.api.v1 import agent_tasks as agent_tasks_module
    source = await _read_source(agent_tasks_module.__file__)
    assert "Phase2.8A 兜底 dispatch" not in source
    assert "Worker 未启动" not in source


@pytest.mark.asyncio
async def test_sse_subscribes_to_runtime_live_event_bus_and_unregisters():
    """LangGraph events must reach SSE through the runtime event bus."""
    request = _FakeRequest()
    bus = _FakeLiveEventBus()
    request.app.state.live_event_bus = bus

    queue, unsubscribe = await _subscribe_task_events(
        request=request,
        task_id="task-live-bus",
    )

    assert bus.subscriptions == [("task-live-bus", queue)]
    await unsubscribe()
    assert bus.unsubscriptions == [("task-live-bus", queue)]


def test_worker_backed_sse_stays_open_while_task_is_still_running():
    """A quiet worker queue must not terminate a still-running task stream."""
    assert not _should_close_pre_confirm_stream(
        has_local_dispatch_task=False,
        local_dispatch_done=False,
        task_status="running",
    )


def test_worker_backed_sse_stays_open_when_a_failed_task_has_a_queued_retry():
    """A retryable worker failure must not detach the browser before retry."""
    assert not _should_close_pre_confirm_stream(
        has_local_dispatch_task=False,
        local_dispatch_done=False,
        task_status="failed",
        has_pending_execution_retry=True,
    )


@pytest.mark.parametrize(
    "task_status",
    ["waiting_user_confirm", "format_loss_review", "completed", "failed", "cancelled"],
)
def test_worker_backed_sse_closes_only_at_a_pre_confirm_boundary(task_status):
    assert _should_close_pre_confirm_stream(
        has_local_dispatch_task=False,
        local_dispatch_done=False,
        task_status=task_status,
    )


def test_local_dispatch_completion_helper_is_backward_compatible():
    assert _should_close_pre_confirm_stream(
        has_local_dispatch_task=True,
        local_dispatch_done=True,
        task_status="running",
    )


__all__ = [
    "test_sse_does_not_trigger_dispatch_when_worker_attached",
    "test_sse_handler_signature_unchanged",
    "test_sse_has_no_direct_execution_fallback",
    "test_sse_subscribes_to_runtime_live_event_bus_and_unregisters",
    "test_worker_backed_sse_stays_open_while_task_is_still_running",
    "test_worker_backed_sse_closes_only_at_a_pre_confirm_boundary",
    "test_local_dispatch_completion_helper_is_backward_compatible",
]
