"""Structured, correlation-aware application logging primitives."""

from .config import LoggingSettings, logging_settings_from_app_settings
from .context import bind_log_context, clear_log_context, get_log_context, logging_context, unbind_log_context
from .events import EVENT_CATALOG, EventDefinition, LogEvent, log_event, validate_event
from .formatter import JsonFormatter, PrettyConsoleFormatter
from .middleware import RequestLoggingMiddleware, get_request_correlation
from .redaction import redact_text, redact_value
from .setup import setup_logging

__all__ = [
    "JsonFormatter",
    "EVENT_CATALOG",
    "EventDefinition",
    "LogEvent",
    "LoggingSettings",
    "PrettyConsoleFormatter",
    "RequestLoggingMiddleware",
    "bind_log_context",
    "clear_log_context",
    "get_log_context",
    "get_request_correlation",
    "log_event",
    "logging_context",
    "logging_settings_from_app_settings",
    "redact_text",
    "redact_value",
    "setup_logging",
    "unbind_log_context",
    "validate_event",
]
