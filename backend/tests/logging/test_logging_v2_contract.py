from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi import FastAPI

from app.core.logging_system.events import EVENT_CATALOG, EventDefinition, validate_event
from app.core.logging_system.fingerprint import build_error_fingerprint
from app.core.logging_system.formatter import JsonFormatter
from app.core.logging_system.governance import DuplicateSuppressor, LogSizeBudget, SamplePolicy
from app.core.logging_system.middleware import RequestLoggingMiddleware


def test_v2_schema_version_and_catalog_validation():
    assert EVENT_CATALOG["llm.request.completed"].domain == "llm"
    definition = EventDefinition(
        name="example.required",
        domain="application",
        default_level=logging.INFO,
        required_fields=frozenset({"task_id"}),
    )
    assert validate_event(definition, {}) == ("task_id",)
    assert validate_event(definition, {"task_id": "task_1"}) == ()

    formatter = JsonFormatter(environment="development")
    record = logging.LogRecord("testagent.application", logging.INFO, __file__, 1, "ok", (), None)
    record.event = "application.startup.completed"
    payload = json.loads(formatter.format(record))
    assert payload["schema_version"] == "2.0"


def test_error_fingerprint_is_stable_without_correlation_or_secret():
    first = build_error_fingerprint(
        event="llm.request.failed",
        error_domain="llm",
        error_kind="timeout",
        exception_type="TimeoutError",
        error_code="LLM_TIMEOUT",
        stacktrace="Traceback\n  File \"/app/service.py\", line 20, in call\nTimeoutError: API_KEY_SECRET",
    )
    second = build_error_fingerprint(
        event="llm.request.failed",
        error_domain="llm",
        error_kind="timeout",
        exception_type="TimeoutError",
        error_code="LLM_TIMEOUT",
        stacktrace="Traceback\n  File \"/app/service.py\", line 20, in call\nTimeoutError: another secret",
    )
    different = build_error_fingerprint(
        event="llm.request.failed",
        error_domain="llm",
        error_kind="connection",
        exception_type="ConnectionError",
        error_code="LLM_CONNECTION",
        stacktrace="Traceback\n  File \"/app/service.py\", line 20, in call\nConnectionError",
    )
    assert first == second
    assert first != different
    assert len(first) == 16


def test_sampling_is_deterministic_and_never_drops_errors():
    policy = SamplePolicy(enabled=True, success_sample_rate=0.0, health_sample_rate=0.0)
    assert policy.should_emit(level=logging.ERROR, event="llm.request.failed", fields={"trace_id": "a"})
    assert policy.should_emit(level=logging.WARNING, event="security.invalid_trace_context", fields={"trace_id": "a"})
    assert not policy.should_emit(level=logging.INFO, event="http.request.completed", fields={"trace_id": "a"})
    assert policy.should_emit(level=logging.INFO, event="http.request.completed", fields={"trace_id": "a"}) is False


def test_size_budget_preserves_identity_and_truncates_large_values():
    budget = LogSizeBudget(max_event_bytes=250)
    payload = budget.apply(
        {
            "event": "context.compose.completed",
            "level": "INFO",
            "request_id": "req_1",
            "trace_id": "a" * 32,
            "message": "x" * 2000,
            "unexpected": "y" * 2000,
        }
    )
    assert payload["log_truncated"] is True
    assert payload["original_size_bytes"] > 250
    assert payload["event"] == "context.compose.completed"
    assert payload["trace_id"] == "a" * 32
    assert len(json.dumps(payload, ensure_ascii=False).encode()) <= 500


def test_duplicate_suppressor_keeps_first_and_produces_summary_after_window():
    suppressor = DuplicateSuppressor(window_seconds=60)
    assert suppressor.register("dependency.request.failed", "abc")[0] is True
    assert suppressor.register("dependency.request.failed", "abc")[0] is False


def test_sensitive_event_allowlist_blocks_prompt_and_key():
    formatter = JsonFormatter(environment="development")
    record = logging.LogRecord("testagent.integration", logging.INFO, __file__, 1, "completed", (), None)
    record.event = "llm.request.completed"
    record.provider = "provider"
    record.model = "model"
    record.duration_ms = 12
    record.prompt = "PROMPT_SECRET_SENTINEL"
    record.api_key = "API_KEY_SECRET_SENTINEL"
    payload = json.loads(formatter.format(record))
    encoded = json.dumps(payload)
    assert "PROMPT_SECRET_SENTINEL" not in encoded
    assert "API_KEY_SECRET_SENTINEL" not in encoded
    assert payload["provider"] == "provider"


@pytest.mark.asyncio
async def test_middleware_continues_valid_traceparent_and_replaces_invalid_one():
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/trace")
    async def trace_endpoint():
        from app.core.logging_system.context import get_log_context
        return get_log_context()

    transport = httpx.ASGITransport(app=app)
    valid = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        continued = await client.get("/trace", headers={"traceparent": valid})
        replaced = await client.get("/trace", headers={"traceparent": "invalid"})

    assert continued.headers["x-request-id"] == continued.json()["request_id"]
    assert continued.json()["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert continued.headers["traceparent"].split("-")[1] == "0123456789abcdef0123456789abcdef"
    assert replaced.headers["x-request-id"] == replaced.json()["request_id"]
    assert len(replaced.json()["trace_id"]) == 32
    assert replaced.json()["trace_id"] != "invalid"
