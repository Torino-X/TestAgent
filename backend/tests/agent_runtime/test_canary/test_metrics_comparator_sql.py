"""Phase 2.7 — MetricsComparator SQL 模板测试。

构造 fake session 模拟 7 个 SQL 路径;不连真实 MySQL。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from app.agent_runtime.canary.metrics_comparator import MetricsComparator


@dataclass
class FakeRow:
    values: tuple


class FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class FakeSession:
    def __init__(self, plan: list[FakeResult]) -> None:
        self._plan = list(plan)
        self.executed_sql: list[str] = []

    async def execute(self, stmt: Any, params: dict | None = None) -> Any:
        sql = str(stmt).lower()
        self.executed_sql.append(sql)
        if not self._plan:
            return FakeResult([])
        return self._plan.pop(0)


class FakeSessionFactory:
    def __init__(self, sess: FakeSession) -> None:
        self._s = sess

    def __call__(self) -> Any:
        @asynccontextmanager
        async def cm():
            yield self._s

        return cm()


def _build_session(plan: list[FakeResult]) -> FakeSession:
    return FakeSession(plan)


def test_compare_returns_mirror_structure_with_legacy_and_langgraph() -> None:
    sess = _build_session([
        FakeResult([
            ("legacy", 100, 95, 5, 90, 0.90, 60.0, 60.0),
            ("langgraph", 30, 28, 4, 26, 0.85, 75.0, 75.0),
        ]),
        FakeResult([
            ("langgraph", 1.5),
        ]),
        FakeResult([]),  # artifacts
        FakeResult([]),  # tool_calls
        FakeResult([]),  # agent_events
    ])
    mc = MetricsComparator(session_factory=FakeSessionFactory(sess))

    async def _run() -> dict:
        return await mc.compare(window_minutes=60)

    result = asyncio.run(_run())

    assert "legacy" in result and "langgraph" in result
    assert result["legacy"]["task_count"] == 100
    assert result["langgraph"]["task_count"] == 30
    assert result["legacy"]["success_rate"] == 0.95
    assert result["langgraph"]["success_rate"] == 0.9333333333 or abs(result["langgraph"]["success_rate"] - (28/30)) < 1e-6
    assert result["langgraph"]["avg_token_cost_usd"] == 1.5
    assert result["gap"]["task_count"] == 30 - 100


def test_compare_handles_zero_task_count() -> None:
    sess = _build_session([
        FakeResult([]),  # 主聚合 → 空
        FakeResult([]),  # token
        FakeResult([]),
        FakeResult([]),
        FakeResult([]),
    ])
    mc = MetricsComparator(session_factory=FakeSessionFactory(sess))

    async def _run() -> dict:
        return await mc.compare(window_minutes=60)

    result = asyncio.run(_run())
    # 双引擎都给空(或者默认值)
    assert result["legacy"]["task_count"] == 0
    assert result["langgraph"]["task_count"] == 0


def test_compare_handles_token_without_engine_type() -> None:
    """``agent_runs`` 仅 langgraph 默认 server_default,legacy 不写 run,正常应为 0。"""
    sess = _build_session([
        FakeResult([
            ("legacy", 50, 49, 1, 0, 0.0, 0.0, 0.0),
        ]),
        FakeResult([
            ("langgraph", 0.3),
        ]),
        FakeResult([]),
        FakeResult([]),
        FakeResult([]),
    ])
    mc = MetricsComparator(session_factory=FakeSessionFactory(sess))

    async def _run() -> dict:
        return await mc.compare(window_minutes=60)

    result = asyncio.run(_run())
    assert result["legacy"]["task_count"] == 50
    assert result["langgraph"]["avg_token_cost_usd"] == 0.3
    assert result["langgraph"]["task_count"] == 0


def test_compare_window_must_be_positive() -> None:
    mc = MetricsComparator(session_factory=FakeSessionFactory(_build_session([])))
    try:
        asyncio.run(mc.compare(window_minutes=0))
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_compare_saves_artifact(tmp_path: str) -> None:
    sess = _build_session([
        FakeResult([]),
        FakeResult([]),
        FakeResult([]),
        FakeResult([]),
        FakeResult([]),
    ])
    mc = MetricsComparator(session_factory=FakeSessionFactory(sess))

    async def _run() -> str:
        return await mc.save_artifact(output_dir=tmp_path, payload={"legacy": {"task_count": 0}})

    path = asyncio.run(_run())
    assert path.endswith(".json")
    import os

    assert os.path.exists(path)
    md = path.replace(".json", ".md")
    assert os.path.exists(md)
