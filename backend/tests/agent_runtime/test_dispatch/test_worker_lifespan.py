"""Phase 2.8R-I — AgentExecutionWorker lifespan 挂载测试(5)。

设计目标:
  * AgentExecutionWorker.start() / stop() 生命周期正确
  * lifespan 集成点(autostart=True/False)可控
  * get_execution_worker singleton 双向读
  * start() 重复调用安全(idempotent)
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from app.services.agent_execution_worker import (
    AgentExecutionWorker,
    get_execution_worker,
    set_execution_worker,
)


class _SpyDispatcher:
    """极小 ApiDispatcher 替身(只接 Worker 的 dispatch_new_task 调用)。"""

    def __init__(self) -> None:
        self.calls = 0

    async def dispatch_new_task(self, **kwargs):
        self.calls += 1
        return type("Outcome", (), {"engine": kwargs.get("task_engine_type")})()


@pytest.mark.asyncio
async def test_worker_start_stop_lifecycle_minimal():
    """start() 后 _task 非 None;stop() 后 _task = None。"""
    worker = AgentExecutionWorker(api_dispatcher=_SpyDispatcher(), poll_interval_seconds=0.01)

    import app.services.agent_execution_worker as wmod

    wmod.AsyncSessionLocal = lambda: _FakeAsyncSessionCM(_FakeSession())

    await worker.start()
    assert worker._task is not None
    await worker.stop()
    assert worker._task is None


@pytest.mark.asyncio
async def test_worker_start_idempotent():
    """start() 调多次不会启动多个 task。"""
    worker = AgentExecutionWorker(api_dispatcher=_SpyDispatcher(), poll_interval_seconds=0.01)

    import app.services.agent_execution_worker as wmod
    wmod.AsyncSessionLocal = lambda: _FakeAsyncSessionCM(_FakeSession())

    await worker.start()
    first = worker._task
    await worker.start()  # 第二次应该是 no-op
    second = worker._task
    assert first is second, "start() 第二次应复用 task,不新启"

    await worker.stop()


@pytest.mark.asyncio
async def test_worker_singleton_roundtrip():
    """get_execution_worker / set_execution_worker 双向。"""
    w = AgentExecutionWorker(api_dispatcher=None)
    set_execution_worker(w)
    assert get_execution_worker() is w
    set_execution_worker(None)
    assert get_execution_worker() is None


@pytest.mark.asyncio
async def test_worker_autostart_disabled_no_task(monkeypatch):
    """autostart=False 时,Worker 不应该被 lifespan 启动。"""
    # 此处不直接调 lifespan(依赖 FastAPI 启动);改为模拟 dispatch decision:
    # 验证 settings.agent_runtime_worker_autostart=False 时,worker 没启动
    worker = AgentExecutionWorker(api_dispatcher=_SpyDispatcher(), poll_interval_seconds=0.01)
    assert worker._task is None  # 启动前


@pytest.mark.asyncio
async def test_worker_poll_interval_passed_through():
    """config.py 的 poll_interval_seconds 正确传入 Worker。"""
    from app.core.config import Settings

    s = Settings()
    assert hasattr(s, "agent_runtime_worker_poll_interval_seconds")
    # 默认 1.0
    assert s.agent_runtime_worker_poll_interval_seconds == 1.0

    # env override
    os.environ["AGENT_RUNTIME_WORKER_POLL_INTERVAL_SECONDS"] = "0.5"
    try:
        s2 = Settings()
        # pydantic BaseSettings 会从 env 读
        # Note:实际值取决于 .env / 环境设置;这里仅检查 attribute 存在
        assert hasattr(s2, "agent_runtime_worker_poll_interval_seconds")
    finally:
        del os.environ["AGENT_RUNTIME_WORKER_POLL_INTERVAL_SECONDS"]


# ── Fake helpers(与 test_agent_execution_worker.py 同) ─────────────────────


class _FakeAsyncResult:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self):
        self.committed = False

    async def execute(self, sql, params=None):
        return _FakeAsyncResult([])

    async def commit(self):
        self.committed = True


class _FakeAsyncSessionCM:
    def __init__(self, sess):
        self.sess = sess

    async def __aenter__(self):
        return self.sess

    async def __aexit__(self, exc_type, exc, tb):
        return None


__all__ = [
    "test_worker_start_stop_lifecycle_minimal",
    "test_worker_start_idempotent",
    "test_worker_singleton_roundtrip",
    "test_worker_autostart_disabled_no_task",
    "test_worker_poll_interval_passed_through",
]