"""Regression coverage for public task-id forwarding in SSE status checks."""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from app.api.v1 import agent_tasks


@pytest.mark.parametrize(
    "endpoint",
    [
        agent_tasks.task_events_sse,
        agent_tasks.task_events_incremental_sse,
    ],
)
def test_sse_status_reads_forward_the_route_task_id(endpoint) -> None:
    """Every heartbeat/final read must use the route's public ``task_id``.

    The nested event-stream generators close over ``task_id``.  Referencing a
    copied local named ``task_public_id`` instead is not detected until the
    first 30-second heartbeat timeout, where it raises ``NameError`` and tears
    down the SSE response.
    """

    tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))
    status_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_read_task_status_for_sse"
    ]

    assert len(status_calls) == 2
    for call in status_calls:
        public_id_keyword = next(
            keyword
            for keyword in call.keywords
            if keyword.arg == "task_public_id"
        )
        assert isinstance(public_id_keyword.value, ast.Name)
        assert public_id_keyword.value.id == "task_id"
