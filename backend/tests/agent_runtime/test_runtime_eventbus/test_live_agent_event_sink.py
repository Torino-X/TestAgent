"""Phase 2.6 — LiveAgentEventSink 3-stage fan-out (alloc → DB idempotent → bus publish)."""

from __future__ import annotations

import asyncio
import pytest
from datetime import datetime

from app.agent_runtime.events.live_agent_event_sink import LiveAgentEventSink
from app.agent_runtime.events.live_event_bus import InMemoryLiveEventBus


class _FakeAllocator:
    """Fake SequenceNumberAllocator for sink tests."""

    def __init__(self, start: int = 1) -> None:
        self._n = start - 1

    async def next(self, *, task_internal_id: int) -> int:
        self._n += 1
        return self._n

    async def next_batch(self, *, task_internal_id: int, count: int) -> list[int]:
        self._n += count
        return list(range(self._n - count + 1, self._n + 1))


class _FakeRepo:
    """Fake EventRepository capturing create_with_idempotency calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_with_idempotency(self, *, event_id: str, idempotency_key, event) -> bool:
        self.calls.append(
            {
                "event_id": event_id,
                "idempotency_key": idempotency_key,
                "sequence_no": event.sequence_no,
                "node_name": event.node_name,
            }
        )
        return True


class _NullSessionFactory:
    def __call__(self) -> "_NullCM":
        return _NullCM()


class _NullCM:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_sink_emits_event_with_allocated_sequence_and_publishes_to_bus() -> None:
    alloc = _FakeAllocator(start=1)
    repo = _FakeRepo()
    bus = InMemoryLiveEventBus()
    queue: asyncio.Queue = asyncio.Queue()
    await bus.subscribe(task_id="42", queue=queue)

    sink = LiveAgentEventSink(
        session_factory=_NullSessionFactory(),
        event_repo=repo,
        allocator=alloc,  # type: ignore[arg-type]
        bus=bus,
        task_internal_id=42,
        conversation_internal_id=99,
        user_internal_id=7,
        graph_run_id="gr-1",
        graph_version="v2",
    )

    event_dict = await sink.emit(
        task_id="42",
        node_name="parse_requirement",
        event_type="tool_finished",
        title="parsed",
        content="ok",
        payload={"duration_ms": 12},
    )

    # sequence assigned
    assert event_dict["sequence_no"] == 1
    assert event_dict["event_type"] == "tool_finished"
    assert event_dict["node_name"] == "parse_requirement"
    # DB row written
    assert len(repo.calls) == 1
    assert repo.calls[0]["sequence_no"] == 1
    # Bus fan-out
    live_event = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert live_event["event_type"] == "tool_finished"
    assert live_event["sequence_no"] == 1


@pytest.mark.asyncio
async def test_sink_idempotency_key_format_matches_compute_idempotency_key() -> None:
    alloc = _FakeAllocator()
    repo = _FakeRepo()
    bus = InMemoryLiveEventBus()
    sink = LiveAgentEventSink(
        session_factory=_NullSessionFactory(),
        event_repo=repo,
        allocator=alloc,  # type: ignore[arg-type]
        bus=bus,
        task_internal_id=42,
        conversation_internal_id=99,
        user_internal_id=7,
        graph_run_id="gr-1",
    )
    await sink.emit(
        task_id="42",
        node_name="node_a",
        event_type="tool_finished",
        title="t",
        content="c",
    )
    key = repo.calls[0]["idempotency_key"]
    assert key == "42|gr-1|node_a|tool_finished|1"


@pytest.mark.asyncio
async def test_sink_uses_caller_provided_sequence_no() -> None:
    alloc = _FakeAllocator()
    repo = _FakeRepo()
    bus = InMemoryLiveEventBus()
    sink = LiveAgentEventSink(
        session_factory=_NullSessionFactory(),
        event_repo=repo,
        allocator=alloc,  # type: ignore[arg-type]
        bus=bus,
        task_internal_id=42,
        conversation_internal_id=99,
        user_internal_id=7,
        graph_run_id="gr-1",
    )
    event_dict = await sink.emit(
        task_id="42",
        node_name="n",
        event_type="x",
        title="t",
        content="c",
        sequence_no=99,
    )
    assert event_dict["sequence_no"] == 99
    assert repo.calls[0]["sequence_no"] == 99


@pytest.mark.asyncio
async def test_sink_swallows_db_failure_and_still_publishes() -> None:
    """DB 写失败仅 log,不抛;事件仍可推 bus."""

    class _BoomRepo:
        async def create_with_idempotency(self, **kw: object) -> bool:
            raise RuntimeError("db down")

    class _BoomAllocator:
        async def next(self, *, task_internal_id: int) -> int:
            return 1

    bus = InMemoryLiveEventBus()
    queue: asyncio.Queue = asyncio.Queue()
    await bus.subscribe(task_id="42", queue=queue)
    sink = LiveAgentEventSink(
        session_factory=_NullSessionFactory(),
        event_repo=_BoomRepo(),
        allocator=_BoomAllocator(),  # type: ignore[arg-type]
        bus=bus,
        task_internal_id=42,
        conversation_internal_id=99,
        user_internal_id=7,
        graph_run_id="gr-1",
    )
    event_dict = await sink.emit(
        task_id="42",
        node_name="n",
        event_type="x",
        title="t",
        content="c",
    )
    assert event_dict["sequence_no"] == 1
    # Bus still received it
    live_event = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert live_event["event_type"] == "x"


@pytest.mark.asyncio
async def test_sink_publishes_to_public_task_channel_when_provided() -> None:
    alloc = _FakeAllocator()
    repo = _FakeRepo()
    bus = InMemoryLiveEventBus()
    queue: asyncio.Queue = asyncio.Queue()
    await bus.subscribe(task_id="task_public_42", queue=queue)

    sink = LiveAgentEventSink(
        session_factory=_NullSessionFactory(),
        event_repo=repo,
        allocator=alloc,  # type: ignore[arg-type]
        bus=bus,
        task_internal_id=42,
        task_public_id="task_public_42",
        conversation_internal_id=99,
        user_internal_id=7,
        graph_run_id="gr-1",
    )

    event_dict = await sink.emit(
        task_id="task_public_42",
        node_name="parse_requirement",
        event_type="tool_finished",
        title="parsed",
        content="ok",
    )

    assert event_dict["task_id"] == "task_public_42"
    live_event = await asyncio.wait_for(queue.get(), timeout=0.5)
    assert live_event["task_id"] == "task_public_42"
