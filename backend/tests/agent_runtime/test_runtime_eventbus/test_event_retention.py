"""Phase 2.6 — EventRetentionPolicy summary / batch cap / 2-stage cleanup."""

from __future__ import annotations

from datetime import datetime, timedelta
import pytest
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime.events.event_retention import (
    DEFAULT_CONFIG,
    EventRetentionConfig,
    EventRetentionPolicy,
)


class _FakeScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar(self) -> Any:
        return self._value


class _FakeRowsResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows


class _FakeResult:
    def __init__(self, rowcount: int = 0) -> None:
        self.rowcount = rowcount


class _FakeAsyncSession(AsyncSession):
    """继承 AsyncSession 才能通过 source 模块内的 isinstance 守门。"""

    def __init__(self) -> None:  # type: ignore[no-untyped-def]
        self.executed: list[tuple[str, dict]] = []
        self.committed = 0
        self.rolled_back = 0
        # bypass parent __init__
        self.bind = None
        self.sync_session = None

    async def execute(self, stmt: object, params: dict | None = None) -> object:
        sql_text = str(stmt)
        self.executed.append((sql_text, params or {}))
        if "DELETE FROM agent_events" in sql_text and "created_at <" in sql_text:
            return _FakeResult(0)
        if "GROUP BY task_id" in sql_text and "HAVING n" in sql_text:
            return _FakeRowsResult([])
        if "SELECT MIN(id) FROM" in sql_text:
            return _FakeScalarResult(None)
        return _FakeResult(0)

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:
        self.rolled_back += 1


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
async def test_cleanup_once_returns_summary_with_config() -> None:
    factory = _FakeSessionFactory()
    policy = EventRetentionPolicy(session_factory=factory, config=DEFAULT_CONFIG)
    summary = await policy.cleanup_once()
    assert "deleted_by_age" in summary
    assert "deleted_by_per_task" in summary
    assert "total_deleted" in summary
    assert summary["config"]["keep_last_days"] == DEFAULT_CONFIG.keep_last_days
    assert summary["config"]["keep_last_per_task"] == DEFAULT_CONFIG.keep_last_per_task
    # ran_at timestamp 是 iso 格式
    assert "T" in summary["ran_at"]


@pytest.mark.asyncio
async def test_cleanup_once_runs_both_stages() -> None:
    factory = _FakeSessionFactory()
    policy = EventRetentionPolicy(session_factory=factory, config=DEFAULT_CONFIG)
    await policy.cleanup_once()

    sqls = [sql for sql, _ in factory.session.executed]
    # 阶段 1: 按天数清理
    assert any("created_at <" in s and "DELETE" in s for s in sqls)
    # 阶段 2: per-task 计数
    assert any("GROUP BY task_id" in s for s in sqls)


@pytest.mark.asyncio
async def test_cleanup_once_respects_keep_days_and_batch_cap() -> None:
    factory = _FakeSessionFactory()
    cfg = EventRetentionConfig(keep_last_days=7, keep_last_per_task=1000, max_delete_batch=500)
    policy = EventRetentionPolicy(session_factory=factory, config=cfg)
    summary = await policy.cleanup_once()
    assert summary["config"]["keep_last_days"] == 7
    assert summary["config"]["keep_last_per_task"] == 1000
    assert summary["config"]["max_delete_batch"] == 500

    # 第一阶段 DELETE 用了我们传的 max_delete_batch=500
    sqls_params = [(sql, params) for sql, params in factory.session.executed]
    age_delete = next(
        (p for s, p in sqls_params if "DELETE" in s and "created_at <" in s),
        None,
    )
    assert age_delete is not None
    assert age_delete["lim"] == 500


def test_event_retention_config_to_dict_round_trip() -> None:
    cfg = EventRetentionConfig(keep_last_days=14, keep_last_per_task=2000)
    d = cfg.to_dict()
    assert d["keep_last_days"] == 14
    assert d["keep_last_per_task"] == 2000
    assert d["protect_last_terminated_per_task"] is True