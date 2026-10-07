"""Phase 2.6 — DistributedCancellationService idempotency / cache / cross-bus signal."""

from __future__ import annotations

import pytest
from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.cancellation_distributed import DistributedCancellationService


class _FakeScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar(self) -> Any:
        return self._value


class _FakeResult:
    def __init__(self, rowcount: int = 0) -> None:
        self.rowcount = rowcount


class _FakeAsyncSession(AsyncSession):
    """继承 AsyncSession 才能通过 service 内的 isinstance 守门。"""

    def __init__(self, already_cancelled: bool = False) -> None:  # type: ignore[no-untyped-def]
        self.already_cancelled = already_cancelled
        self.executed: list[tuple[str, dict]] = []
        self.committed = 0
        self.rolled_back = 0
        # bypass parent __init__
        self.bind = None
        self.sync_session = None

    async def execute(self, stmt: Any, params: dict | None = None) -> Any:
        sql_text = str(stmt)
        self.executed.append((sql_text, params or {}))
        if "UPDATE agent_tasks" in sql_text and "runtime_status" in sql_text:
            if self.already_cancelled:
                return _FakeResult(0)
            return _FakeResult(1)
        if "INSERT INTO agent_events" in sql_text:
            return _FakeResult(1)
        if "SELECT runtime_status" in sql_text:
            return _FakeScalarResult("cancelled" if self.already_cancelled else "running")
        return _FakeScalarResult(None)

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        self.rolled_back += 1


class _FakeSessionFactory:
    def __init__(self, already_cancelled: bool = False) -> None:
        self.session = _FakeAsyncSession(already_cancelled=already_cancelled)

    def __call__(self) -> "_FakeCM":
        return _FakeCM(self.session)


class _FakeCM:
    def __init__(self, session: _FakeAsyncSession) -> None:
        self._s = session

    async def __aenter__(self) -> _FakeAsyncSession:
        return self._s

    async def __aexit__(self, *args: object) -> None:
        return None


class _CapturingBus:
    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish(self, *, task_id: str, event: dict) -> None:
        self.published.append({"task_id": task_id, "event": event})


@pytest.mark.asyncio
async def test_cancel_first_call_returns_true_and_emits_bus_signal() -> None:
    factory = _FakeSessionFactory(already_cancelled=False)
    bus = _CapturingBus()
    svc = DistributedCancellationService(session_factory=factory, bus=bus)

    result = await svc.cancel(task_id="42", source="user")
    assert result is True
    # UPDATE + INSERT agent_events
    assert any("UPDATE agent_tasks" in sql for sql, _ in factory.session.executed)
    assert any("INSERT INTO agent_events" in sql for sql, _ in factory.session.executed)
    # Bus publish
    assert len(bus.published) == 1
    payload = bus.published[0]["event"]
    assert payload["event_type"] == "cancel_observed"
    assert payload["task_id"] == "42"


@pytest.mark.asyncio
async def test_cancel_second_call_returns_false_skips_db_and_publish() -> None:
    """After local cache populated, second cancel returns False fast."""

    factory = _FakeSessionFactory(already_cancelled=False)
    bus = _CapturingBus()
    svc = DistributedCancellationService(session_factory=factory, bus=bus)

    first = await svc.cancel(task_id="42", source="user")
    factory.session.executed.clear()
    bus.published.clear()

    second = await svc.cancel(task_id="42", source="user")
    assert first is True
    assert second is False
    assert factory.session.executed == []
    assert bus.published == []


@pytest.mark.asyncio
async def test_cancel_when_db_already_cancelled_returns_false() -> None:
    factory = _FakeSessionFactory(already_cancelled=True)
    bus = _CapturingBus()
    svc = DistributedCancellationService(session_factory=factory, bus=bus)
    result = await svc.cancel(task_id="42", source="user")
    assert result is False
    # bus 也不该推(因为 DB UPDATE rowcount=0 → 已取消,直接返回)
    assert bus.published == []


@pytest.mark.asyncio
async def test_is_cancelled_uses_local_cache_after_cancel() -> None:
    factory = _FakeSessionFactory(already_cancelled=False)
    svc = DistributedCancellationService(session_factory=factory, bus=None)
    assert await svc.is_cancelled(task_id="42") is False
    await svc.cancel(task_id="42", source="user")
    assert await svc.is_cancelled(task_id="42") is True


@pytest.mark.asyncio
async def test_is_cancelled_returns_true_when_db_says_cancelled() -> None:
    factory = _FakeSessionFactory(already_cancelled=True)
    svc = DistributedCancellationService(session_factory=factory, bus=None)
    assert await svc.is_cancelled(task_id="42") is True


@pytest.mark.asyncio
async def test_is_cancelled_invalid_task_id_returns_false() -> None:
    factory = _FakeSessionFactory()
    svc = DistributedCancellationService(session_factory=factory, bus=None)
    assert await svc.is_cancelled(task_id="not-an-int") is False


def test_is_cancelled_legacy_uses_local_cache_only() -> None:
    """``is_cancelled_legacy`` 同步版只读本地 cache;避免 sync → async 改造."""

    factory = _FakeSessionFactory(already_cancelled=False)
    svc = DistributedCancellationService(session_factory=factory, bus=None)

    # 未取消 → False
    assert svc.is_cancelled_legacy("42") is False


@pytest.mark.asyncio
async def test_cancel_db_failure_returns_false_and_does_not_publish() -> None:
    class _BoomSession(_FakeAsyncSession):
        def __init__(self) -> None:
            super().__init__()

        async def execute(self, stmt: Any, params: dict | None = None) -> Any:
            raise RuntimeError("db down")

    class _BoomFactory:
        def __call__(self) -> "_BoomCM":
            return _BoomCM()

    class _BoomCM:
        async def __aenter__(self) -> _BoomSession:
            return _BoomSession()

        async def __aexit__(self, *a: object) -> None:
            return None

    bus = _CapturingBus()
    svc = DistributedCancellationService(session_factory=_BoomFactory(), bus=bus)
    assert await svc.cancel(task_id="42", source="user") is False
    # Bus 未推(DB 失败不该推)
    assert bus.published == []
    # 本地 cache 未污染
    assert svc.is_cancelled_legacy("42") is False


@pytest.mark.asyncio
async def test_clear_resets_local_cache() -> None:
    factory = _FakeSessionFactory(already_cancelled=False)
    svc = DistributedCancellationService(session_factory=factory, bus=None)
    await svc.cancel(task_id="42", source="user")
    assert svc.is_cancelled_legacy("42") is True
    svc.clear("42")
    assert svc.is_cancelled_legacy("42") is False