from __future__ import annotations

from app.agent_runtime.langgraph_run_lifecycle import resolve_outcome_task_status


class Outcome:
    def __init__(
        self,
        *,
        paused: bool = False,
        completed: bool = False,
        task_status: str | None = None,
    ) -> None:
        self.paused = paused
        self.completed = completed
        self.task_status = task_status


def test_paused_outcome_defaults_to_waiting_for_user_confirmation() -> None:
    assert resolve_outcome_task_status(Outcome(paused=True)) == "waiting_user_confirm"


def test_completed_outcome_defaults_to_completed() -> None:
    assert resolve_outcome_task_status(Outcome(completed=True)) == "completed"


def test_explicit_outcome_status_wins_over_default() -> None:
    assert (
        resolve_outcome_task_status(Outcome(paused=True, task_status="custom_waiting"))
        == "custom_waiting"
    )
