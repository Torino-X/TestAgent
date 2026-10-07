"""Phase 2.8R-B — AgentExecutionWorker 单测(4 测试)。

设计目标(对应 docs/35 §2.3 + 验收二):
  * Worker 单次轮询 = 0 行 → 不抛错
  * Worker 委派 dispatcher.dispatch_new_task(spy)
  * Worker 处理异常时调 release_lease_for_retry 不 mark_failed
  * start / stop 生命周期(背景 task 启动 + 清理)

本测试用最小 FakeSession + SpyDispatcher,不依赖真实 DB / Redis / LLM。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from app.services.agent_execution_worker import (
    AgentExecutionWorker,
    get_execution_worker,
    set_execution_worker,
)


def test_mysql_connection_errors_use_bounded_exponential_backoff():
    """A transient cloud-MySQL outage must not be retried once per second."""
    from app.services.agent_execution_worker import database_retry_delay_seconds
    from sqlalchemy.exc import OperationalError

    exc = OperationalError(
        "connect",
        {},
        type("MysqlError", (), {"args": (2003, "Can't connect to MySQL server")})(),
    )

    assert database_retry_delay_seconds(exc, failure_count=1, poll_interval=1.0) == 1.0
    assert database_retry_delay_seconds(exc, failure_count=2, poll_interval=1.0) == 2.0
    assert database_retry_delay_seconds(exc, failure_count=9, poll_interval=1.0) == 30.0
    assert database_retry_delay_seconds(RuntimeError("other"), failure_count=1, poll_interval=1.0) is None


# ── Spy dispatcher (mirror contract api_dispatcher.dispatch_new_task) ──


class _SpyDispatcher:
    def __init__(self):
        self.dispatched: list = []
        self.dispatch_session_commit_states: list[bool] = []
        self.dispatch_sessions: list[Any] = []
        self.fail_once = False

    async def dispatch_new_task(self, *, task_public_id, task_engine_type, context):
        self.dispatched.append((task_public_id, task_engine_type, context))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated dispatch failure")
        return type("Outcome", (), {"engine": task_engine_type})()

    async def dispatch_from_outbox_row(self, *, row, session):
        """Phase 2.8R-K — Worker 新调用路径。"""
        # row 是 AsyncMock(测试);从调用方 args/kwargs 拿 public_id
        public_id = getattr(row, "public_id", None) or "task_test_unknown"
        engine_type = getattr(row, "engine_type", None) or "langgraph"
        self.dispatch_session_commit_states.append(session.committed)
        self.dispatch_sessions.append(session)
        self.dispatched.append((public_id, engine_type, None))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated dispatch failure")
        return type("Outcome", (), {"engine": engine_type})()


class _FakeAsyncResult:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """极简 FakeSession — claim_next / mark_completed / mark_failed 都走 no-op。"""

    def __init__(self):
        self.committed = False
        self.rolled_back = False
        self._exec_log: list = []

    async def execute(self, sql, params=None):
        self._exec_log.append((str(sql), params))
        # 默认返回空(无 candidate)
        return _FakeAsyncResult([])

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_poll_once_empty_queue_does_not_raise():
    """空队列 → claim_next 返回 None → poll_once 不抛错。"""
    worker = AgentExecutionWorker(api_dispatcher=_SpyDispatcher(), poll_interval_seconds=0.01)

    # 替换 AsyncSessionLocal 的导入,确保不再去拿真实 DB
    import app.services.agent_execution_worker as worker_module
    original_local = worker_module.AsyncSessionLocal
    worker_module.AsyncSessionLocal = lambda: _FakeSessionCM(_FakeSession())
    try:
        n = await worker._poll_once()
    finally:
        worker_module.AsyncSessionLocal = original_local

    assert n == 0


@pytest.mark.asyncio
async def test_worker_dispatches_via_dispatcher_when_claimed():
    """claim_next 拿到行 → Worker 委派到 dispatcher.dispatch_new_task。"""
    spy = _SpyDispatcher()
    worker = AgentExecutionWorker(api_dispatcher=spy, poll_interval_seconds=0.01)

    # Monkey-patch claim_next → 返回预构造行
    fake_row = type(
        "Row",
        (),
        {
            "id": 1,
            "task_id": 200,
            "public_id": "task-public-1",
            "request_type": "new_task",
            "engine_type": "langgraph",
            "graph_name": None,
            "graph_version": None,
            "payload_json": {"foo": "bar"},
        },
    )()

    async def fake_claim_next(*args, **kwargs):
        return fake_row

    import app.services.agent_execution_worker as worker_module
    fake_session = _FakeSessionWithRow(fake_row)
    worker_module.AsyncSessionLocal = lambda: _FakeSessionCM(fake_session)

    # Monkey-patch resolve_task_public_id
    async def fake_resolve(session, tid):
        return "task-public-1"
    worker._resolve_task_public_id = fake_resolve

    # Patch repo method we don't have a real one — 直接 stub 仓库行为
    from app.repositories import agent_execution_request_repository as repo_module
    original_claim = repo_module.AgentExecutionRequestRepository.claim_next
    original_mark_completed = repo_module.AgentExecutionRequestRepository.mark_completed

    async def claim_with_row(self, **kwargs):
        return fake_row

    async def mark_noop(self, **kwargs):
        return 1

    repo_module.AgentExecutionRequestRepository.claim_next = claim_with_row
    repo_module.AgentExecutionRequestRepository.mark_completed = mark_noop
    try:
        n = await worker._poll_once()
    finally:
        repo_module.AgentExecutionRequestRepository.claim_next = original_claim
        repo_module.AgentExecutionRequestRepository.mark_completed = original_mark_completed

    assert n == 1
    assert len(spy.dispatched) == 1
    tid, eng, ctx = spy.dispatched[0]
    assert tid == "task-public-1"
    assert eng == "langgraph"
    assert ctx is None  # Phase 2.8R-K:Worker 不再传 dict ctx
    assert spy.dispatch_session_commit_states == [True]


@pytest.mark.asyncio
async def test_worker_handles_dispatch_failure_via_release_lease():
    """dispatcher 抛异常 → Worker 走 release_lease_for_retry,不 mark_failed。"""
    spy = _SpyDispatcher()
    spy.fail_once = True
    worker = AgentExecutionWorker(api_dispatcher=spy, poll_interval_seconds=0.01)

    fake_row = type(
        "Row",
        (),
        {
            "id": 7,
            "task_id": 200,
            "request_type": "new_task",
            "engine_type": "langgraph",
            "graph_name": None,
            "graph_version": None,
            "payload_json": None,
        },
    )()
    fake_session = _FakeSessionWithRow(fake_row)

    import app.services.agent_execution_worker as worker_module
    worker_module.AsyncSessionLocal = lambda: _FakeSessionCM(fake_session)

    async def fake_resolve(session, tid):
        return "task-public-1"
    worker._resolve_task_public_id = fake_resolve

    from app.repositories import agent_execution_request_repository as repo_module
    original_claim = repo_module.AgentExecutionRequestRepository.claim_next
    original_release = repo_module.AgentExecutionRequestRepository.release_lease_for_retry
    original_mark_failed = repo_module.AgentExecutionRequestRepository.mark_failed

    release_called = {"count": 0}
    mark_failed_called = {"count": 0}

    async def claim_with_row(self, **kwargs):
        return fake_row

    async def release_counting(self, **kwargs):
        release_called["count"] += 1
        return 1

    async def mark_failed_counting(self, **kwargs):
        mark_failed_called["count"] += 1
        return 1

    repo_module.AgentExecutionRequestRepository.claim_next = claim_with_row
    repo_module.AgentExecutionRequestRepository.release_lease_for_retry = release_counting
    repo_module.AgentExecutionRequestRepository.mark_failed = mark_failed_counting
    try:
        n = await worker._poll_once()
    finally:
        repo_module.AgentExecutionRequestRepository.claim_next = original_claim
        repo_module.AgentExecutionRequestRepository.release_lease_for_retry = original_release
        repo_module.AgentExecutionRequestRepository.mark_failed = original_mark_failed

    assert n == 1
    assert release_called["count"] == 1
    assert mark_failed_called["count"] == 0
    assert fake_session.rolled_back is True


@pytest.mark.asyncio
async def test_worker_dispatches_with_a_fresh_session_after_claim_commit():
    """Long-running dispatch must not retain the claim transaction session."""
    spy = _SpyDispatcher()
    worker = AgentExecutionWorker(api_dispatcher=spy, poll_interval_seconds=0.01)
    fake_row = type(
        "Row",
        (),
        {
            "id": 9,
            "task_id": 200,
            "public_id": "task-public-9",
            "request_type": "new_task",
            "engine_type": "langgraph",
            "graph_name": None,
            "graph_version": None,
            "payload_json": {},
        },
    )()
    claim_session = _FakeSessionWithRow(fake_row)
    dispatch_session = _FakeSession()
    completion_session = _FakeSession()
    sessions = [claim_session, dispatch_session, completion_session]

    import app.services.agent_execution_worker as worker_module
    original_local = worker_module.AsyncSessionLocal
    worker_module.AsyncSessionLocal = lambda: _FakeSessionCM(sessions.pop(0))

    async def fake_resolve(session, tid):
        return "task-public-9"

    worker._resolve_task_public_id = fake_resolve
    from app.repositories import agent_execution_request_repository as repo_module
    original_claim = repo_module.AgentExecutionRequestRepository.claim_next
    original_mark_completed = repo_module.AgentExecutionRequestRepository.mark_completed

    async def claim_with_row(self, **kwargs):
        return fake_row

    async def mark_noop(self, **kwargs):
        return 1

    repo_module.AgentExecutionRequestRepository.claim_next = claim_with_row
    repo_module.AgentExecutionRequestRepository.mark_completed = mark_noop
    try:
        assert await worker._poll_once() == 1
    finally:
        worker_module.AsyncSessionLocal = original_local
        repo_module.AgentExecutionRequestRepository.claim_next = original_claim
        repo_module.AgentExecutionRequestRepository.mark_completed = original_mark_completed

    assert spy.dispatch_sessions == [dispatch_session]
    assert claim_session.committed is True
    assert completion_session.committed is True


@pytest.mark.asyncio
async def test_worker_dispatches_incremental_task_via_outbox_row():
    """incremental_task 必须由 Worker 派发到 dispatcher,不能落入 unknown type。"""
    spy = _SpyDispatcher()
    worker = AgentExecutionWorker(api_dispatcher=spy, poll_interval_seconds=0.01)
    fake_row = type(
        "Row",
        (),
        {
            "id": 13,
            "task_id": 201,
            "public_id": "exq-task-inc-1-incremental_task",
            "request_type": "incremental_task",
            "engine_type": "langgraph",
            "graph_name": "incremental_test_plan",
            "graph_version": "v1",
            "payload_json": {
                "request_type": "incremental_task",
                "incremental_intent": {"scope": {"kind": "modify_section"}},
            },
        },
    )()
    claim_session = _FakeSessionWithRow(fake_row)
    dispatch_session = _FakeSession()
    completion_session = _FakeSession()
    sessions = [claim_session, dispatch_session, completion_session]

    import app.services.agent_execution_worker as worker_module
    original_local = worker_module.AsyncSessionLocal
    worker_module.AsyncSessionLocal = lambda: _FakeSessionCM(sessions.pop(0))

    async def fake_resolve(session, tid):
        return "task-inc-1"

    worker._resolve_task_public_id = fake_resolve
    from app.repositories import agent_execution_request_repository as repo_module
    original_claim = repo_module.AgentExecutionRequestRepository.claim_next
    original_mark_completed = repo_module.AgentExecutionRequestRepository.mark_completed
    original_mark_failed = repo_module.AgentExecutionRequestRepository.mark_failed

    mark_failed_called = {"count": 0}

    async def claim_with_row(self, **kwargs):
        return fake_row

    async def mark_completed_noop(self, **kwargs):
        return 1

    async def mark_failed_counting(self, **kwargs):
        mark_failed_called["count"] += 1
        return 1

    repo_module.AgentExecutionRequestRepository.claim_next = claim_with_row
    repo_module.AgentExecutionRequestRepository.mark_completed = mark_completed_noop
    repo_module.AgentExecutionRequestRepository.mark_failed = mark_failed_counting
    try:
        assert await worker._poll_once() == 1
    finally:
        worker_module.AsyncSessionLocal = original_local
        repo_module.AgentExecutionRequestRepository.claim_next = original_claim
        repo_module.AgentExecutionRequestRepository.mark_completed = original_mark_completed
        repo_module.AgentExecutionRequestRepository.mark_failed = original_mark_failed

    assert len(spy.dispatched) == 1
    tid, eng, ctx = spy.dispatched[0]
    assert tid == "exq-task-inc-1-incremental_task"
    assert eng == "langgraph"
    assert ctx is None
    assert spy.dispatch_sessions == [dispatch_session]
    assert mark_failed_called["count"] == 0


@pytest.mark.asyncio
async def test_worker_start_stop_lifecycle():
    """start() → 跑后台 _run_forever;stop() 后 _task=None。"""
    spy = _SpyDispatcher()
    worker = AgentExecutionWorker(api_dispatcher=spy, poll_interval_seconds=0.01)

    # 用 _FakeSession 替换 DB
    import app.services.agent_execution_worker as worker_module
    worker_module.AsyncSessionLocal = lambda: _FakeSessionCM(_FakeSession())

    await worker.start()
    await asyncio.sleep(0.05)  # 让循环跑几轮
    assert worker._task is not None

    await worker.stop()
    assert worker._task is None


# ── helpers ─────────────────────────────────────────────────────────────────


class _FakeSessionCM:
    """Async context manager wrapping a FakeSession."""

    def __init__(self, sess):
        self.sess = sess

    async def __aenter__(self):
        return self.sess

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakeSessionWithRow(_FakeSession):
    def __init__(self, row):
        super().__init__()
        self._row = row

    async def execute(self, sql, params=None):
        # claim_next 的 SELECT 部分返回空,我们的 monkey-patch 直接让
        # repo.claim_next 返回 fake_row ——
        # 所以这里随便返回
        return _FakeAsyncResult([])


def test_singleton_helper_roundtrip():
    """get_execution_worker / set_execution_worker 双向存读。"""
    w = AgentExecutionWorker(api_dispatcher=None)
    set_execution_worker(w)
    assert get_execution_worker() is w
    set_execution_worker(None)
    assert get_execution_worker() is None


__all__ = [
    "test_worker_poll_once_empty_queue_does_not_raise",
    "test_worker_dispatches_via_dispatcher_when_claimed",
    "test_worker_handles_dispatch_failure_via_release_lease",
    "test_worker_dispatches_with_a_fresh_session_after_claim_commit",
    "test_worker_dispatches_incremental_task_via_outbox_row",
    "test_worker_start_stop_lifecycle",
    "test_singleton_helper_roundtrip",
]
