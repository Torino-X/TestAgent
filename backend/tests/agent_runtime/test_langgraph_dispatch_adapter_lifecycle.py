from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app.agent_runtime.langgraph_dispatch_adapter import LangGraphDispatchAdapter
from app.agent_runtime.langgraph_run_coordinator import RunOutcome


@dataclass
class PreparedRun:
    graph_run_id: str


class FakeLifecycle:
    def __init__(self) -> None:
        self.prepared: list[tuple[str, str | None]] = []
        self.outcomes: list[tuple[str, str, RunOutcome]] = []
        self.failures: list[tuple[str, str, BaseException]] = []

    async def prepare(self, task_public_id: str, *, preferred_graph_run_id: str | None = None) -> PreparedRun:
        self.prepared.append((task_public_id, preferred_graph_run_id))
        return PreparedRun(graph_run_id=preferred_graph_run_id or f"durable-{task_public_id}")

    async def apply_outcome(self, task_public_id: str, graph_run_id: str, outcome: RunOutcome) -> None:
        self.outcomes.append((task_public_id, graph_run_id, outcome))

    async def mark_failed(self, task_public_id: str, graph_run_id: str, error: BaseException) -> None:
        self.failures.append((task_public_id, graph_run_id, error))


class FakeCoordinator:
    def __init__(self, result: RunOutcome | BaseException) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def run_pre_confirm(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def make_outcome(*, completed: bool = False, paused: bool = True) -> RunOutcome:
    return RunOutcome(
        completed=completed,
        paused=paused,
        pause_marker="section_confirmation" if paused else None,
        task_status="waiting_user_confirm" if paused else "completed",
        final_state={},
        current_node="waiting_user_confirm" if paused else "end",
    )


@pytest.mark.asyncio
async def test_new_task_uses_durable_run_id_and_persists_pause_outcome() -> None:
    lifecycle = FakeLifecycle()
    outcome = make_outcome()
    coordinator = FakeCoordinator(outcome)
    adapter = LangGraphDispatchAdapter(coordinator, lifecycle=lifecycle)

    result = await adapter.run_pre_confirm(SimpleNamespace(task_id="task_001"))

    assert result is outcome
    assert lifecycle.prepared == [("task_001", None)]
    assert coordinator.calls[0]["graph_run_id"] == "durable-task_001"
    assert lifecycle.outcomes == [("task_001", "durable-task_001", outcome)]


@pytest.mark.asyncio
async def test_resume_reuses_the_explicit_durable_run_id() -> None:
    lifecycle = FakeLifecycle()
    outcome = make_outcome(completed=True, paused=False)
    coordinator = FakeCoordinator(outcome)
    adapter = LangGraphDispatchAdapter(coordinator, lifecycle=lifecycle)

    await adapter.run_pre_confirm(SimpleNamespace(task_id="task_001", graph_run_id="run_existing"))

    assert lifecycle.prepared == [("task_001", "run_existing")]
    assert coordinator.calls[0]["graph_run_id"] == "run_existing"
    assert lifecycle.outcomes == [("task_001", "run_existing", outcome)]


@pytest.mark.asyncio
async def test_adapter_marks_durable_run_failed_when_coordinator_raises() -> None:
    lifecycle = FakeLifecycle()
    error = RuntimeError("graph crashed")
    adapter = LangGraphDispatchAdapter(FakeCoordinator(error), lifecycle=lifecycle)

    with pytest.raises(RuntimeError, match="graph crashed"):
        await adapter.run_pre_confirm(SimpleNamespace(task_id="task_001"))

    assert lifecycle.failures[0][0:2] == ("task_001", "durable-task_001")
    assert lifecycle.failures[0][2] is error
