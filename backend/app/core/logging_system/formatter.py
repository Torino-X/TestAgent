"""JSONL and human-readable formatters sharing one safe record schema."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .context import get_log_context
from .events import filter_event_fields
from .governance import LogSizeBudget
from .redaction import MASK, is_sensitive_key, redact_text, redact_value

_STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, *, environment: str = "development", schema_version: str = "2.0", max_event_bytes: int = 32 * 1024) -> None:
        super().__init__()
        self._environment = environment
        self._schema_version = schema_version
        self._budget = LogSizeBudget(max_event_bytes=max_event_bytes)

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload = self._payload(record)
            return json.dumps(self._budget.apply(payload), ensure_ascii=False, default=str, separators=(",", ":"))
        except Exception as exc:  # pragma: no cover - safety net for malformed third-party records
            return json.dumps({"timestamp": _now(), "schema_version": self._schema_version, "level": "ERROR", "event": "logging.formatter.failed", "message": redact_text(exc)})

    def _payload(self, record: logging.LogRecord) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp": _now(),
            "schema_version": self._schema_version,
            "level": record.levelname,
            "service": getattr(record, "service", "testagent-backend"),
            "environment": getattr(record, "environment", self._environment),
            "logger": record.name,
            "event": getattr(record, "event", "log.record"),
            "message": redact_text(record.getMessage()),
        }
        for key, value in get_log_context().items():
            if value is None:
                continue
            payload[key] = MASK if is_sensitive_key(key) else redact_value(value)
        try:
            from app.core.observability.tracing import current_span_id, current_trace_id
            trace_id, span_id = current_trace_id(), current_span_id()
        except Exception:  # tracing must remain fail-open
            trace_id, span_id = None, None
        if trace_id and "trace_id" not in payload:
            payload["trace_id"] = trace_id
        if span_id and "span_id" not in payload:
            payload["span_id"] = span_id
        raw_fields: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_") and value is not None:
                raw_fields[key] = MASK if is_sensitive_key(key) else redact_value(value)
        payload.update(filter_event_fields(str(payload["event"]), raw_fields))
        payload.setdefault("process_id", getattr(record, "process_id", record.process))
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            payload["exception_message"] = redact_text(record.exc_info[1])
            payload["stacktrace"] = redact_text(self.formatException(record.exc_info))
        return payload


class PrettyConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(fmt="%(levelname)-7s | %(asctime)s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
