"""Deterministic sampling, duplicate suppression and size budgets."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SamplePolicy:
    enabled: bool = True
    success_sample_rate: float = 1.0
    health_sample_rate: float = 0.01

    def should_emit(self, *, level: int, event: str, fields: dict[str, Any]) -> bool:
        if not self.enabled or level >= logging.ERROR or event.startswith("security.") or event.endswith(".failed"):
            return True
        rate = self.health_sample_rate if event.startswith("health.") else self.success_sample_rate
        if rate >= 1:
            return True
        if rate <= 0:
            return False
        key = str(fields.get("trace_id") or fields.get("task_id") or fields.get("request_id") or event)
        return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF < rate


@dataclass(slots=True)
class DuplicateSuppressor:
    window_seconds: int = 60
    _seen: dict[tuple[str, str], tuple[float, int]] = field(default_factory=dict)

    def register(self, event: str, fingerprint: str | None) -> tuple[bool, dict[str, Any] | None]:
        if not fingerprint:
            return True, None
        key = (event, fingerprint)
        now = time.monotonic()
        prior = self._seen.get(key)
        if prior is None or now - prior[0] >= self.window_seconds:
            summary = None if prior is None or prior[1] == 0 else {
                "event": "logging.suppressed.summary", "suppressed_event": event,
                "error_fingerprint": fingerprint, "suppressed_count": prior[1], "window_seconds": self.window_seconds,
            }
            self._seen[key] = (now, 0)
            return True, summary
        self._seen[key] = (prior[0], prior[1] + 1)
        return False, None


@dataclass(frozen=True, slots=True)
class LogSizeBudget:
    max_event_bytes: int = 32 * 1024

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= self.max_event_bytes:
            return payload
        required = ("timestamp", "schema_version", "level", "service", "environment", "logger", "event", "request_id", "trace_id", "span_id", "task_id", "error_fingerprint", "error_domain", "error_kind", "error_type", "error_code", "retryable")
        compact = {key: payload[key] for key in required if key in payload}
        compact.update({"message": "[truncated]", "log_truncated": True, "original_size_bytes": len(encoded)})
        return compact
