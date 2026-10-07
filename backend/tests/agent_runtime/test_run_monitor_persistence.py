from __future__ import annotations

from datetime import timezone
from types import SimpleNamespace

import pytest

from app.agent_runtime.observability import run_monitor
from app.agent_runtime.observability.run_monitor import RunMonitor, _default_clock


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class FakeRunRepository:
    created = []
    runs = {"run_existing": SimpleNamespace(id=7)}
    token_updates = []
    status_updates = []
    finish_updates = []

    def __init__(self, _session) -> None:
        pass

    async def create(self, run):
        self.created.append(run)
        return run

    async def get_by_public_id(self, public_id: str):
        return self.runs.get(public_id)

    async def append_token_usage(self, **kwargs):
        self.token_updates.append(kwargs)

    async def set_status(self, **kwargs):
        self.status_updates.append(kwargs)

    async def finish(self, **kwargs):
        self.finish_updates.append(kwargs)


@pytest.fixture(autouse=True)
def replace_repository(monkeypatch):
    FakeRunRepository.created = []
    FakeRunRepository.token_updates = []
    FakeRunRepository.status_updates = []
    FakeRunRepository.finish_updates = []
    monkeypatch.setattr(run_monitor, "AgentRunRepository", FakeRunRepository)


@pytest.mark.asyncio
async def test_start_commits_its_durable_run_and_keeps_utc_timezone() -> None:
    session = FakeSession()
    monitor = RunMonitor(session_factory=lambda: session)

    run = await monitor.start(task_internal_id=12)

    assert run is not None
    assert session.commits == 1
    assert run.started_at.tzinfo is timezone.utc


@pytest.mark.asyncio
async def test_token_and_finish_updates_commit_their_independent_sessions() -> None:
    token_session = FakeSession()
    finish_session = FakeSession()
    sessions = iter((token_session, finish_session))
    monitor = RunMonitor(session_factory=lambda: next(sessions))

    await monitor.append_tokens(
        run_public_id="run_existing",
        profile="task_generation",
        prompt_tokens=5,
        completion_tokens=8,
    )
    await monitor.finish(run_public_id="run_existing", success=False, error_message="boom")

    assert token_session.commits == 1
    assert finish_session.commits == 1
    assert FakeRunRepository.token_updates[0]["run_internal_id"] == 7
    assert FakeRunRepository.finish_updates[0]["status"] == "failed"


def test_default_clock_is_timezone_aware_utc() -> None:
    assert _default_clock().tzinfo is timezone.utc
