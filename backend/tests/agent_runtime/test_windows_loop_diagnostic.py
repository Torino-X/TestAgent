"""Tests for the Psycopg event-loop diagnostic."""

from __future__ import annotations

import logging

from app.agent_runtime.persistence import postgres_checkpointer


def test_proactor_running_loop_is_rejected_with_launcher_guidance(
    monkeypatch, caplog
) -> None:
    class FakeProactorLoop:
        pass

    monkeypatch.setattr(postgres_checkpointer.sys, "platform", "win32")
    monkeypatch.setattr(
        postgres_checkpointer.asyncio, "get_running_loop", lambda: FakeProactorLoop()
    )
    monkeypatch.setattr(
        postgres_checkpointer.asyncio, "ProactorEventLoop", FakeProactorLoop
    )

    with caplog.at_level(logging.INFO):
        compatible = postgres_checkpointer._running_loop_is_psycopg_compatible()

    assert compatible is False
    assert "running event loop = FakeProactorLoop" in caplog.text
    assert "python scripts/run_dev.py" in caplog.text


def test_non_proactor_running_loop_is_accepted(monkeypatch) -> None:
    class FakeSelectorLoop:
        pass

    monkeypatch.setattr(postgres_checkpointer.sys, "platform", "win32")
    monkeypatch.setattr(
        postgres_checkpointer.asyncio, "get_running_loop", lambda: FakeSelectorLoop()
    )

    assert postgres_checkpointer._running_loop_is_psycopg_compatible() is True
