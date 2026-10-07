"""Phase 2.6 — RunMonitor start/append_tokens/finish/list_node_metrics."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from app.agent_runtime.observability.run_monitor import RunMonitor


class _FakeScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar(self) -> Any:
        return self._value

    def fetchall(self) -> Any:
        return self._value


class _FakeResult:
    def __init__(self, value: Any = 0) -> None:
        self._value = value
        self.rowcount = value if isinstance(value, int) else 0

    def scalar(self) -> Any:
        return self._value

    def fetchall(self) -> Any:
        return self._value


class _FakeRunObj:
    """ORM-like AgentRun stub."""

    def __init__(self, public_id: str, task_id: int) -> None:
        self.public_id = public_id
        self.task_id = task_id
        self.status = "created"
        self.token_usage_json: dict = {}
        self.error_json: dict | None = None
        self.started_at = None
        self.finished_at = None
        self.id = 1001


class _FakeAgentRunRepository:
    """Fake AgentRunRepository whose ``create`` returns an obj with .public_id."""

    def __init__(self, session: "_FakeAsyncSession") -> None:
        self.session = session
        self._next_id = 1000

    async def create(self, run: Any) -> Any:
        self.session.added.append(run)
        run.id = self._next_id
        self._next_id += 1
        self.session._run_obj = run
        return run

    async def get_by_public_id(self, public_id: str) -> Any:
        return self.session._run_obj

    async def get_by_internal_id(self, internal_id: int) -> Any:
        return self.session._run_obj

    async def list_by_task(self, task_internal_id: int, *, limit: int = 20) -> list:
        return [self.session._run_obj] if self.session._run_obj else []

    async def set_status(self, *, run_internal_id: int, status: str, error_json=None) -> None:
        self.session.executed.append(("UPDATE agent_runs SET status=...", {"rid": run_internal_id, "st": status}))
        if self.session._run_obj:
            self.session._run_obj.status = status
            if error_json is not None:
                self.session._run_obj.error_json = error_json

    async def finish(self, *, run_internal_id: int, status: str, finished_at) -> None:
        self.session.executed.append(("UPDATE agent_runs SET status=..., finished_at=...", {"rid": run_internal_id, "st": status}))
        if self.session._run_obj:
            self.session._run_obj.status = status
            self.session._run_obj.finished_at = finished_at

    async def append_token_usage(self, *, run_internal_id: int, profile: str, prompt_tokens: int, completion_tokens: int) -> dict:
        self.session.executed.append(("UPDATE agent_runs SET token_usage_json=JSON_SET...", {
            "rid": run_internal_id, "profile_key": profile,
            "pt": prompt_tokens, "ct": completion_tokens,
        }))
        return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens}

    async def accumulate_cost_estimate(self, *, run_internal_id: int, delta_usd: float) -> None:
        self.session.executed.append(("UPDATE agent_runs SET token_usage_json ... cost_estimate_usd", {"rid": run_internal_id, "d": delta_usd}))


class _FakeAsyncSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.executed: list[tuple[str, dict]] = []
        self.committed = 0
        self.flushed = 0
        self.refreshed: list[Any] = []
        self._run_obj: Any = None

    async def execute(self, stmt: Any, params: dict | None = None) -> Any:
        sql_text = str(stmt)
        self.executed.append((sql_text, params or {}))
        if "LAST_INSERT_ID" in sql_text:
            return _FakeScalarResult(9999)
        return _FakeScalarResult(None)

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        self._run_obj = obj

    async def flush(self) -> None:
        self.flushed += 1

    async def refresh(self, obj: Any) -> None:
        self.refreshed.append(obj)

    async def commit(self) -> None:
        self.committed += 1


class _FakeSessionFactory:
    def __init__(self) -> None:
        self.session = _FakeAsyncSession()

    def __call__(self) -> "_FakeCM":
        return _FakeCM(self.session)


class _FakeCM:
    def __init__(self, session: _FakeAsyncSession) -> None:
        self._s = session

    async def __aenter__(self) -> _FakeAsyncSession:
        return self._s

    async def __aexit__(self, *a: object) -> None:
        return None


@pytest.mark.asyncio
async def test_run_monitor_start_creates_run_with_running_status(monkeypatch) -> None:
    """``start`` 返回 ORM-like 对象,且 status='running'."""

    factory = _FakeSessionFactory()
    rm = RunMonitor(session_factory=factory)

    # Patch AgentRunRepository 在 run_monitor 模块作用域内的引用
    from app.agent_runtime.observability import run_monitor as rm_mod

    monkeypatch.setattr(rm_mod, "AgentRunRepository", _FakeAgentRunRepository)

    run = await rm.start(
        task_internal_id=42,
        engine_type="langgraph",
        graph_name="test_plan_generation",
        graph_version="v2",
        thread_id="42",
    )
    assert run is not None
    assert run.task_id == 42
    # AgentRunRepository.create 调了 session.flush + LAST_INSERT_ID;不调 commit
    # 关键是返回的对象带 id 与 public_id,且 status="running"
    assert run.status == "running"
    assert run.id == 1000  # _FakeAgentRunRepository._next_id 起始值


@pytest.mark.asyncio
async def test_run_monitor_append_tokens_calls_repository(monkeypatch) -> None:
    factory = _FakeSessionFactory()
    rm = RunMonitor(session_factory=factory)

    from app.agent_runtime.observability import run_monitor as rm_mod
    monkeypatch.setattr(rm_mod, "AgentRunRepository", _FakeAgentRunRepository)

    await rm.start(
        task_internal_id=42,
        engine_type="langgraph",
        graph_name="g",
        graph_version="v2",
        thread_id="t",
    )
    factory.session.executed.clear()

    # get_by_public_id 返回上面创建的 run;append_token_usage 会被调用
    await rm.append_tokens(
        run_public_id="run_xyz",
        profile="preparation_agent",
        prompt_tokens=100,
        completion_tokens=50,
    )
    # 关键是 DB 没抛 + run 状态不变


@pytest.mark.asyncio
async def test_run_monitor_finish_writes_status_and_finished_at(monkeypatch) -> None:
    factory = _FakeSessionFactory()
    rm = RunMonitor(session_factory=factory)

    from app.agent_runtime.observability import run_monitor as rm_mod
    monkeypatch.setattr(rm_mod, "AgentRunRepository", _FakeAgentRunRepository)

    await rm.start(
        task_internal_id=42,
        engine_type="langgraph",
        graph_name="g",
        graph_version="v2",
        thread_id="t",
    )
    factory.session.executed.clear()
    await rm.finish(run_public_id="run_xyz", success=True)
    # 调了 set_status + finish;两个 UPDATE
    assert len(factory.session.executed) >= 2


@pytest.mark.asyncio
async def test_run_monitor_list_node_metrics_aggregates_by_node_name(monkeypatch) -> None:
    # 替换 list_node_metrics 走原生 SQL
    rows_result = [
        ("parse_requirement", 4, "2026-01-01 00:00:00", "2026-01-01 00:01:00"),
        ("generate_test_plan", 1, "2026-01-01 00:01:00", "2026-01-01 00:02:00"),
    ]

    class _RowsSession(_FakeAsyncSession):
        async def execute(self, stmt: Any, params: dict | None = None) -> Any:
            sql_text = str(stmt)
            if "GROUP BY node_name" in sql_text:
                return _FakeResult(rows_result)
            return await super().execute(stmt, params)

    class _RowsFactory(_FakeSessionFactory):
        def __init__(self) -> None:
            self.session = _RowsSession()

    factory2 = _RowsFactory()
    rm2 = RunMonitor(session_factory=factory2)
    rows = await rm2.list_node_metrics(task_internal_id=42)
    assert isinstance(rows, list)
    assert len(rows) == 2
    assert rows[0]["node_name"] == "parse_requirement"
    assert rows[0]["event_count"] == 4


@pytest.mark.asyncio
async def test_run_monitor_failure_writes_error_dict(monkeypatch) -> None:
    factory = _FakeSessionFactory()
    rm = RunMonitor(session_factory=factory)

    from app.agent_runtime.observability import run_monitor as rm_mod
    monkeypatch.setattr(rm_mod, "AgentRunRepository", _FakeAgentRunRepository)

    await rm.start(
        task_internal_id=42,
        engine_type="langgraph",
        graph_name="g",
        graph_version="v2",
        thread_id="t",
    )
    await rm.finish(
        run_public_id="run_xyz",
        success=False,
        error_code="tool_failed",
        error_message="TestPlanGeneratorTool returned success=False",
    )
    # finish 会写 error_json
    assert factory.session._run_obj.error_json is not None
    assert factory.session._run_obj.error_json["code"] == "tool_failed"
    assert "TestPlanGeneratorTool" in factory.session._run_obj.error_json["message"]


@pytest.mark.asyncio
async def test_run_monitor_db_failure_does_not_propagate(monkeypatch) -> None:
    """任何 DB 异常仅 log,不抛(节点不应因 monitor 失败失败)."""

    class _BoomFactory:
        def __call__(self) -> "_BoomCM":
            return _BoomCM()

    class _BoomCM:
        async def __aenter__(self) -> Any:
            raise RuntimeError("db down")

        async def __aexit__(self, *a: object) -> None:
            return None

    rm = RunMonitor(session_factory=_BoomFactory())
    # 不抛
    run = await rm.start(
        task_internal_id=42,
        engine_type="langgraph",
        graph_name="g",
        graph_version="v2",
        thread_id="t",
    )
    # start 失败 → 返回 None
    assert run is None