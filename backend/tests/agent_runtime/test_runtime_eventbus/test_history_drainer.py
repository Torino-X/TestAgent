"""Phase 2.6 — HistoryDrainer parse_last_event_id + drain replay + dedup + control frame."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.sse.history_drainer import (
    HistoryDrainer,
    parse_last_event_id,
)


class _FakeRow:
    def __init__(self, *values: Any) -> None:
        self._v = values

    def __getitem__(self, idx: int) -> Any:
        return self._v[idx]


class _FakeScalarResult:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def fetchall(self) -> list[_FakeRow]:
        return self._rows


class _FakeAsyncSession(AsyncSession):
    def __init__(self, rows: list[_FakeRow] | None = None) -> None:  # type: ignore[no-untyped-def]
        self.rows = rows or []
        self.last_sql: str | None = None
        self.last_params: dict | None = None
        # bypass parent __init__
        self.bind = None
        self.sync_session = None

    async def execute(self, stmt: Any, params: dict | None = None) -> Any:
        self.last_sql = str(stmt)
        self.last_params = params or {}
        return _FakeScalarResult(self.rows)


class _FakeSessionFactory:
    def __init__(self, session: _FakeAsyncSession) -> None:
        self._s = session

    def __call__(self) -> "_FakeCM":
        return _FakeCM(self._s)


class _FakeCM:
    def __init__(self, session: _FakeAsyncSession) -> None:
        self._s = session

    async def __aenter__(self) -> _FakeAsyncSession:
        return self._s

    async def __aexit__(self, *a: object) -> None:
        return None


class _FakeBus:
    def __init__(self) -> None:
        self.subs: list[asyncio.Queue] = []
        self.unsubs: list[asyncio.Queue] = []
        self.to_publish: list[dict] = []

    async def subscribe(self, *, task_id: str, queue: asyncio.Queue) -> asyncio.Queue:
        self.subs.append(queue)
        return queue

    async def unsubscribe(self, *, task_id: str, queue: asyncio.Queue) -> None:
        self.unsubs.append(queue)


def test_parse_last_event_id_int_returns_sequence_no() -> None:
    out = parse_last_event_id("42")
    assert out["sequence_no"] == 42
    assert out["public_id"] is None


def test_parse_last_event_id_uuid_returns_public_id() -> None:
    out = parse_last_event_id("01J7XYZ...")
    assert out["sequence_no"] is None
    assert out["public_id"] == "01J7XYZ..."


def test_parse_last_event_id_empty_or_none_returns_both_none() -> None:
    assert parse_last_event_id(None) == {"sequence_no": None, "public_id": None}
    assert parse_last_event_id("") == {"sequence_no": None, "public_id": None}
    assert parse_last_event_id("   ") == {"sequence_no": None, "public_id": None}


@pytest.mark.asyncio
async def test_drain_replays_history_then_emits_task_replay_complete() -> None:
    rows = [
        _FakeRow(
            "pid-1",  # public_id
            "tool_finished",  # event_type
            "tool",  # message_type
            "t",  # title
            "c",  # content
            '{"k":"v"}',  # payload_json
            "delivered",  # status
            1,  # sequence_no
            "gr-1",  # graph_run_id
            "v2",  # graph_version
            "node_a",  # node_name
            1,  # event_schema_version
            "idem-1",  # idempotency_key
            None,  # created_at
        )
    ]
    session = _FakeAsyncSession(rows=rows)
    factory = _FakeSessionFactory(session)
    bus = _FakeBus()
    drainer = HistoryDrainer(
        session_factory=factory,
        bus=bus,
        task_id="42",
        task_internal_id=42,
    )

    out = []
    async for ev in drainer.drain():
        out.append(ev)
        # break on task_replay_complete so the test doesn't hang on live poll
        if ev.get("event_type") == "task_replay_complete":
            break

    # 第一个是 replay row,第二个是 task_replay_complete
    assert out[0]["event_type"] == "tool_finished"
    assert out[0]["payload"] == {"k": "v"}
    assert out[0]["sequence_no"] == 1
    assert out[1]["event_type"] == "task_replay_complete"
    assert out[1]["payload"]["replayed_count"] == 1
    # SQL 用了 task_id 过滤;Last-Event-ID 没传时无 sequence_no 条件
    assert "WHERE task_id = :tid" in (session.last_sql or "")
    assert "ORDER BY COALESCE(sequence_no, 0) ASC" in (session.last_sql or "")


@pytest.mark.asyncio
async def test_drain_dedups_replayed_event_when_live_republishes() -> None:
    rows = [
        _FakeRow(
            "pid-dup", "tool_finished", "tool", "t", "c",
            "{}", "delivered", 1, "gr-1", "v2", "node_a", 1, "idem-1", None,
        )
    ]
    session = _FakeAsyncSession(rows=rows)
    factory = _FakeSessionFactory(session)
    bus = _FakeBus()
    drainer = HistoryDrainer(
        session_factory=factory,
        bus=bus,
        task_id="42",
        task_internal_id=42,
        live_poll_interval=0.05,
        live_timeout_seconds=0.5,
    )

    # 在 collect 完 replay 后,bus 上的 queue 手动注入一个重复 public_id 事件 + 一个新事件
    captured_queue: asyncio.Queue | None = None

    async def push_after_subscribe() -> None:
        # 等 subscribe 生效,直接拿到 subscribe 端捕获的 queue
        while captured_queue is None:
            await asyncio.sleep(0.01)
        # 重复(replayed set 里有 pid-dup)
        await captured_queue.put({"event_id": "pid-dup", "event_type": "tool_finished"})
        # 新事件
        await captured_queue.put({"event_id": "pid-new", "event_type": "plan_step_completed"})
        await asyncio.sleep(0.05)

    async def subscribe(*, task_id: str, queue: asyncio.Queue) -> asyncio.Queue:
        nonlocal captured_queue
        captured_queue = queue
        bus.subs.append(queue)
        # 模拟发布
        asyncio.create_task(push_after_subscribe())
        return queue

    bus.subscribe = subscribe  # type: ignore[assignment]

    seen_ids = []
    async for ev in drainer.drain(stop_when=lambda e: e.get("event_type") == "plan_step_completed"):
        seen_ids.append(ev.get("event_id") or ev.get("public_id"))

    # replay 1 次 + 新事件 1 次(replayed dedup 把重复的 pid-dup 去掉)
    assert "pid-dup" in seen_ids
    assert "pid-new" in seen_ids
    assert seen_ids.count("pid-dup") == 1


@pytest.mark.asyncio
async def test_drain_replay_failure_continues_with_empty_history() -> None:
    """DB 抛错 → drain 仅写 control frame + 不挂。"""

    class _BoomSession:
        async def execute(self, *a: object, **kw: object) -> Any:
            raise RuntimeError("db down")

    class _BoomFactory:
        def __call__(self) -> "_BoomCM":
            return _BoomCM()

    class _BoomCM:
        async def __aenter__(self) -> _BoomSession:
            return _BoomSession()

        async def __aexit__(self, *a: object) -> None:
            return None

    bus = _FakeBus()
    drainer = HistoryDrainer(
        session_factory=_BoomFactory(),
        bus=bus,
        task_id="42",
        task_internal_id=42,
    )
    out = []
    async for ev in drainer.drain():
        out.append(ev)
        if ev.get("event_type") == "task_replay_complete":
            break
    assert out[-1]["event_type"] == "task_replay_complete"
    assert out[-1]["payload"]["replayed_count"] == 0