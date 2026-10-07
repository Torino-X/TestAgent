"""AgentEventSink - InMemoryEventSink 收集行为。"""

from __future__ import annotations

import pytest

from app.agent_runtime.events.sink import InMemoryEventSink


@pytest.mark.asyncio
async def test_sink_collects_emitted_events() -> None:
    sink = InMemoryEventSink()
    await sink.emit(
        task_id="t-1",
        graph_run_id="r-1",
        node_name="n1",
        event_type="node_started",
        title="Start",
        content="hello",
        payload={"foo": "bar"},
    )
    await sink.emit(
        task_id="t-1",
        graph_run_id="r-1",
        node_name="n1",
        event_type="node_finished",
        title="Done",
        content="bye",
    )
    events = sink.collect()
    assert len(events) == 2
    assert events[0]["event_type"] == "node_started"
    assert events[1]["payload"] == {}
    assert events[1]["task_id"] == "t-1"


@pytest.mark.asyncio
async def test_sink_clear_resets() -> None:
    sink = InMemoryEventSink()
    await sink.emit(
        task_id="t",
        graph_run_id="r",
        node_name="n",
        event_type="x",
        title="x",
        content="x",
    )
    sink.clear()
    assert sink.collect() == []