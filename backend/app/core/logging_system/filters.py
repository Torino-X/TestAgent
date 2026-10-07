"""Record enrichment at the handler boundary."""

from __future__ import annotations

import logging
import os
from typing import Any

from .context import get_log_context
from .fingerprint import fingerprint_from_record
from .governance import DuplicateSuppressor, SamplePolicy
from .redaction import MASK, is_sensitive_key, redact_text, redact_value


class StructuredLogFilter(logging.Filter):
    def __init__(self, *, environment: str, redaction_enabled: bool = True, schema_version: str = "2.0", sampling_enabled: bool = True, success_sample_rate: float = 1.0, health_sample_rate: float = 0.01, rate_limit_enabled: bool = True, rate_limit_window_seconds: int = 60) -> None:
        super().__init__()
        self._environment = environment
        self._redaction_enabled = redaction_enabled
        self._schema_version = schema_version
        self._sample_policy = SamplePolicy(sampling_enabled, success_sample_rate, health_sample_rate)
        self._rate_limit_enabled = rate_limit_enabled
        self._suppressor = DuplicateSuppressor(rate_limit_window_seconds)

    def filter(self, record: logging.LogRecord) -> bool:
        record.environment = self._environment
        record.service = "testagent-backend"
        record.process_id = os.getpid()
        record.schema_version = self._schema_version
        for key, value in get_log_context().items():
            if getattr(record, key, None) is None:
                setattr(record, key, value)
        event = str(getattr(record, "event", "log.record"))
        fields = {key: value for key, value in record.__dict__.items() if key not in _LOG_RECORD_FIELDS and not key.startswith("_")}
        if record.levelno >= logging.ERROR:
            taxonomy, fingerprint = fingerprint_from_record(event, record, fields)
            for key, value in taxonomy.items():
                if getattr(record, key, None) is None and value is not None:
                    setattr(record, key, value)
            if getattr(record, "error_fingerprint", None) is None:
                record.error_fingerprint = fingerprint
            if self._rate_limit_enabled and event != "logging.suppressed.summary":
                emit = getattr(record, "_testagent_rate_allowed", None)
                if emit is None:
                    emit, summary = self._suppressor.register(event, fingerprint)
                    record._testagent_rate_allowed = emit
                    if summary:
                        logging.getLogger("testagent.logging").warning("Duplicate logs suppressed", extra=summary)
                if not emit:
                    return False
        if not self._sample_policy.should_emit(level=record.levelno, event=event, fields={**get_log_context(), **fields}):
            return False
        if self._redaction_enabled:
            try:
                # ``record.msg`` can be a parameterised template such as
                # ``dsn=%s``.  Redacting the template before interpolation
                # removes that placeholder but leaves ``record.args`` intact,
                # causing Python logging to raise a formatting TypeError.
                # Format once, then redact the complete text and clear args
                # so every later handler sees a safe, self-contained record.
                record.msg = redact_text(record.getMessage())
                record.args = ()
                for key, value in tuple(record.__dict__.items()):
                    if key not in _LOG_RECORD_FIELDS and not key.startswith("_"):
                        if is_sensitive_key(key):
                            setattr(record, key, MASK)
                        else:
                            setattr(record, key, redact_value(value))
            except Exception:
                # Redaction itself must not become an application failure.
                pass
        return True


_LOG_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
