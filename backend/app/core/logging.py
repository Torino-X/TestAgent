"""Compatibility facade for the structured TestAgent logging system."""

from app.core.logging_system import (
    JsonFormatter,
    LogEvent,
    LoggingSettings,
    PrettyConsoleFormatter,
    RequestLoggingMiddleware,
    bind_log_context,
    clear_log_context,
    get_log_context,
    get_request_correlation,
    log_event,
    logging_context,
    redact_text,
    redact_value,
    setup_logging,
    unbind_log_context,
)
from app.core.logging_system.filters import StructuredLogFilter

SensitiveFieldFilter = StructuredLogFilter

__all__ = [
    "JsonFormatter", "LoggingSettings", "PrettyConsoleFormatter", "RequestLoggingMiddleware",
    "LogEvent", "SensitiveFieldFilter", "StructuredLogFilter", "bind_log_context", "clear_log_context",
    "get_log_context", "get_request_correlation", "log_event", "logging_context", "redact_text", "redact_value",
    "setup_logging", "unbind_log_context",
]
