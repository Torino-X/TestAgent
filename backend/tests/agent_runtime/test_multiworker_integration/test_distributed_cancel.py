"""Phase 2.6 — Multi-Worker 集成测试 2/3.

**目标**:验证 DistributedCancellationService 在两个 worker 实例间的协调语义。

设计:
- MySQL 是权威取消标志位 —— 我们用 fake session 模拟
- Redis 是实时信号 —— 我们用 shared_pubsub_bus 模拟
- worker-A 调 cancel() → MySQL flag 置位 + Redis 推送 cancel_observed
- worker-B 的 is_cancelled() 走 MySQL SELECT → True
- worker-B 的 raise_if_cancelled() 在下一次循环检查时抛 asyncio.CancelledError
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


class _SharedFakeSession(AsyncSession):
    """模拟"跨进程的同一 MySQL"——所有 worker 看到的 agent_tasks.runtime_status 一样。"""

    def __init__(self, store: dict) -> None:  # type: ignore[no-untyped-def]
        self.store = store
        self.last_sql: str | None = None
        self.last_params: dict | None = None
        self.bind = None
        self.sync_session = None

    async def execute(self, stmt: Any, params: dict | None = None) -> Any:
        sql = str(stmt)
        self.last_sql = sql
        self.last_params = params or {}
        sql_lower = sql.lower()
        if "update agent_tasks" in sql_lower and "runtime_status" in sql_lower:
            tid = params.get("tid") if params else None
            if tid is not None and self.store.get(int(tid), {}).get("runtime_status") != "cancelled":
                self.store.setdefault(int(tid), {})["runtime_status"] = params.get("status", "cancelled")
                return _FakeResult(1)
            return _FakeResult(0)
        if "insert into agent_events" in sql_lower:
            return _FakeResult(1)
        if "select runtime_status" in sql_lower or sql_lower.lstrip().startswith("select"):
            tid = params.get("tid") if params else None
            row = self.store.get(int(tid), {}) if tid is not None else {}
            return _FakeScalarResult(row.get("runtime_status", "running"))
        return _FakeScalarResult(None)

    async def commit(self) -> None:
        pass


class _FakeResult:
    def __init__(self, n: int) -> None:
        self._n = n
        self.rowcount = n


class _FakeScalarResult:
    def __init__(self, v: Any) -> None:
        self._v = v

    def scalar(self) -> Any:
        return self._v


class _FakeSessionCM:
    def __init__(self, session: _SharedFakeSession) -> None:
        self._s = session

    async def __aenter__(self) -> _SharedFakeSession:
        return self._s

    async def __aexit__(self, *a: object) -> None:
        return None


class _SharedSessionFactory:
    def __init__(self, shared: dict) -> None:
        self._s = _SharedFakeSession(shared)

    def __call__(self) -> _FakeSessionCM:
        return _FakeSessionCM(self._s)


@pytest.mark.asyncio
async def test_distributed_cancel_visible_across_workers(shared_pubsub_bus) -> None:
    """worker-A cancel → worker-B 的 is_cancelled 也立即看到(MySQL 共享 row)。"""

    from app.agent_runtime.cancellation_distributed import DistributedCancellationService

    shared: dict = {}
    factory = _SharedSessionFactory(shared)

    # 同一 Redis 共享总线(w1/w2 看同一个 cancel_observed 信号)
    w1 = DistributedCancellationService(session_factory=factory, bus=shared_pubsub_bus)
    w2 = DistributedCancellationService(session_factory=factory, bus=shared_pubsub_bus)

    # 任务初始为 running
    assert await w2.is_cancelled(task_id="99") is False

    # worker-A 取消
    ok = await w1.cancel(task_id="99", source="user")
    assert ok is True

    # worker-B 立刻从 MySQL 看到
    assert await w2.is_cancelled(task_id="99") is True

    # raise_if_cancelled 应抛 asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await w2.raise_if_cancelled(task_id="99")

    # 幂等:再 cancel 返回 False
    assert await w1.cancel(task_id="99", source="user") is False

    await w1.aclose()
    await w2.aclose()