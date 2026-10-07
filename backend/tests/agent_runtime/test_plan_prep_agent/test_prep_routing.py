"""Regression tests for terminal PreparationAgent clarification routing."""

from app.agent_runtime.graphs.test_plan.versions.v3.routing import (
    NODE_PREPARE_CLARIFICATION,
    NODE_SEARCH_KNOWLEDGE,
    NODE_SUGGEST_SECTIONS,
    route_after_preparation,
)


def _ask_user_result() -> dict:
    return {
        "information_sufficient": False,
        "requirement_gaps": [
            {
                "field": "approval_rule",
                "description": "Confirm the approval workflow.",
                "severity": "high",
            }
        ],
        "user_questions": [
            {"field": "approval_rule", "question": "Confirm the approval workflow."}
        ],
    }


def test_ask_user_routes_straight_to_clarification_after_any_retrieval() -> None:
    state = {
        "preparation_result": _ask_user_result(),
        "retrieval_round": 1,
        "retrieval_evidence_bundle": {
            "company_rag": {"status": "skipped", "reason": "not_configured"},
            "project_rag": {"status": "skipped", "reason": "no_project"},
        },
    }

    assert route_after_preparation(state) == NODE_PREPARE_CLARIFICATION


def test_first_retrieval_still_runs_for_new_preparation_query() -> None:
    state = {
        "preparation_result": {
            "information_sufficient": False,
            "queries": ["Find the appointment approval policy."],
        },
        "retrieval_round": 0,
    }

    assert route_after_preparation(state) == NODE_SEARCH_KNOWLEDGE


def test_repeated_retrieval_query_routes_to_clarification() -> None:
    state = {
        "preparation_result": {
            "information_sufficient": False,
            "queries": ["Find the appointment approval policy."],
        },
        "retrieval_round": 1,
        "retrieval_query_signatures": ["find the appointment approval policy."],
    }

    assert route_after_preparation(state) == NODE_PREPARE_CLARIFICATION


def test_submitted_conservative_clarification_beats_a_repeated_query() -> None:
    """A permitted user choice ends this clarification round permanently."""
    state = {
        "preparation_result": {
            "information_sufficient": False,
            "queries": ["Find the appointment approval policy."],
        },
        "retrieval_round": 1,
        "retrieval_query_signatures": ["find the appointment approval policy."],
        "clarification_answers": {
            "answers": {},
            "conservative_gap_ids": ["requirement_scope"],
            "source": "user",
        },
    }

    assert route_after_preparation(state) == NODE_SUGGEST_SECTIONS
