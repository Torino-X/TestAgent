"""Versioned event catalog and safe structured-event facade."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from .redaction import is_sensitive_key

_CORRELATION_FIELDS = frozenset({
    "request_id", "trace_id", "span_id", "task_id", "run_id", "thread_id", "project_id",
    "conversation_id", "message_id", "worker_id", "snapshot_id", "retrieval_run_id",
})
_FORBIDDEN_FIELDS = frozenset({"prompt", "system_prompt", "messages", "completion", "context_body", "memory_content", "document_body", "authorization", "cookie", "api_key", "password", "token"})


@dataclass(frozen=True, slots=True)
class EventDefinition:
    name: str
    domain: str
    default_level: int
    required_fields: frozenset[str] = frozenset()
    optional_fields: frozenset[str] = frozenset()
    sampling_class: str = "normal"
    sensitive_field_policy: str = "redact"


class LogEvent(StrEnum):
    HTTP_REQUEST_STARTED = "http.request.started"
    HTTP_REQUEST_COMPLETED = "http.request.completed"
    HTTP_REQUEST_FAILED = "http.request.failed"
    APPLICATION_STARTUP_STARTED = "application.startup.started"
    APPLICATION_STARTUP_COMPLETED = "application.startup.completed"
    APPLICATION_SHUTDOWN_STARTED = "application.shutdown.started"
    APPLICATION_SHUTDOWN_COMPLETED = "application.shutdown.completed"
    APPLICATION_UNHANDLED_EXCEPTION = "application.unhandled_exception"
    AGENT_TASK_STARTED = "agent.task.started"
    AGENT_TASK_COMPLETED = "agent.task.completed"
    AGENT_TASK_FAILED = "agent.task.failed"
    AGENT_GRAPH_STARTED = "agent.graph.started"
    AGENT_GRAPH_COMPLETED = "agent.graph.completed"
    AGENT_GRAPH_FAILED = "agent.graph.failed"
    AGENT_WORKER_STARTED = "agent.worker.started"
    AGENT_WORKER_COMPLETED = "agent.worker.completed"
    AGENT_WORKER_FAILED = "agent.worker.failed"
    AGENT_NODE_STARTED = "agent.node.started"
    AGENT_NODE_COMPLETED = "agent.node.completed"
    AGENT_NODE_FAILED = "agent.node.failed"
    AGENT_TOOL_STARTED = "agent.tool.started"
    AGENT_TOOL_COMPLETED = "agent.tool.completed"
    AGENT_TOOL_FAILED = "agent.tool.failed"
    CONTEXT_COMPOSE_STARTED = "context.compose.started"
    CONTEXT_COMPOSE_COMPLETED = "context.compose.completed"
    CONTEXT_COMPOSE_FAILED = "context.compose.failed"
    CONTEXT_RETRIEVAL_COMPLETED = "context.retrieval.completed"
    CONTEXT_COMPRESSION_COMPLETED = "context.compression.completed"
    CONTEXT_PAYLOAD_COMPLETED = "context.payload.completed"
    LLM_REQUEST_STARTED = "llm.request.started"
    LLM_REQUEST_COMPLETED = "llm.request.completed"
    LLM_REQUEST_FAILED = "llm.request.failed"
    DEPENDENCY_FAILED = "dependency.request.failed"
    SECURITY_INVALID_TRACE_CONTEXT = "security.invalid_trace_context"
    LOGGING_SCHEMA_INVALID = "logging.schema.invalid"
    LOGGING_SUPPRESSED_SUMMARY = "logging.suppressed.summary"
    SSE_CONNECTED = "agent.sse.connected"
    SSE_DISCONNECTED = "agent.sse.disconnected"


def _definition(event: LogEvent, level: int, *, required: tuple[str, ...] = (), optional: tuple[str, ...] = (), sampling: str = "normal", policy: str = "redact") -> EventDefinition:
    return EventDefinition(str(event), str(event).split(".", 1)[0], level, frozenset(required), frozenset(optional), sampling, policy)


EVENT_CATALOG: dict[str, EventDefinition] = {
    str(event): _definition(event, logging.ERROR if str(event).endswith(".failed") or event is LogEvent.APPLICATION_UNHANDLED_EXCEPTION else logging.INFO)
    for event in LogEvent
}
EVENT_CATALOG.update({
    LogEvent.HTTP_REQUEST_COMPLETED: _definition(LogEvent.HTTP_REQUEST_COMPLETED, logging.INFO, optional=("method", "route", "status_code", "duration_ms"), sampling="success"),
    LogEvent.LLM_REQUEST_STARTED: _definition(LogEvent.LLM_REQUEST_STARTED, logging.INFO, optional=("provider", "model", "call_site", "profile_key", "attempt", "input_tokens", "input_length"), policy="allowlist"),
    LogEvent.LLM_REQUEST_COMPLETED: _definition(LogEvent.LLM_REQUEST_COMPLETED, logging.INFO, optional=("provider", "model", "call_site", "profile_key", "attempt", "duration_ms", "input_tokens", "output_tokens", "total_tokens", "output_length"), sampling="success", policy="allowlist"),
    LogEvent.LLM_REQUEST_FAILED: _definition(LogEvent.LLM_REQUEST_FAILED, logging.ERROR, optional=("provider", "model", "call_site", "profile_key", "attempt", "duration_ms", "error_type", "error_code", "retryable"), policy="allowlist"),
    LogEvent.CONTEXT_COMPOSE_STARTED: _definition(LogEvent.CONTEXT_COMPOSE_STARTED, logging.INFO, optional=("call_site", "profile_key"), policy="allowlist"),
    LogEvent.CONTEXT_COMPOSE_COMPLETED: _definition(LogEvent.CONTEXT_COMPOSE_COMPLETED, logging.INFO, optional=("call_site", "profile_key", "snapshot_id", "sources_selected_count", "selected_count", "dropped_count", "estimated_tokens", "duration_ms", "retrieval_run_id"), policy="allowlist"),
    LogEvent.CONTEXT_COMPOSE_FAILED: _definition(LogEvent.CONTEXT_COMPOSE_FAILED, logging.ERROR, optional=("call_site", "profile_key", "error_type", "error_code", "retryable"), policy="allowlist"),
    LogEvent.SECURITY_INVALID_TRACE_CONTEXT: _definition(LogEvent.SECURITY_INVALID_TRACE_CONTEXT, logging.WARNING, optional=("rule_id", "length", "value_hash"), policy="allowlist"),
})


def validate_event(definition: EventDefinition, fields: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(field for field in definition.required_fields if fields.get(field) is None))


def filter_event_fields(event: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    definition = EVENT_CATALOG.get(event)
    if definition is None or definition.sensitive_field_policy != "allowlist":
        # V1's general redaction contract keeps sensitive keys with a masked
        # value.  Only V2's explicit allowlist policy removes fields entirely.
        return dict(fields)
    allowed = definition.required_fields | definition.optional_fields | _CORRELATION_FIELDS | {"duration_ms", "error_domain", "error_kind", "error_type", "error_code", "retryable", "error_fingerprint"}
    return {key: value for key, value in fields.items() if key in allowed and key not in _FORBIDDEN_FIELDS and not is_sensitive_key(key)}


def log_event(logger: logging.Logger, level: int, event: str | LogEvent, message: str, **fields: Any) -> None:
    event_name = str(event)
    definition = EVENT_CATALOG.get(event_name)
    clean_fields = filter_event_fields(event_name, {key: value for key, value in fields.items() if value is not None})
    if definition is None:
        if os.getenv("LOG_SCHEMA_STRICT", "0").lower() in {"1", "true", "yes"}:
            raise ValueError(f"event is not registered: {event_name}")
        logger.log(logging.WARNING, "Unregistered log event", extra={"event": LogEvent.LOGGING_SCHEMA_INVALID, "invalid_event": event_name, "schema_errors": ("event_not_registered",)})
    else:
        missing = validate_event(definition, clean_fields)
        if missing:
            if os.getenv("LOG_SCHEMA_STRICT", "0").lower() in {"1", "true", "yes"}:
                raise ValueError(f"event {event_name} missing required fields: {', '.join(missing)}")
            logger.log(logging.WARNING, "Invalid log event fields", extra={"event": LogEvent.LOGGING_SCHEMA_INVALID, "invalid_event": event_name, "schema_errors": missing})
    logger.log(level, message, extra={"event": event_name, **clean_fields})
