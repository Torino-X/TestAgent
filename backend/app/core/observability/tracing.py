"""Optional OpenTelemetry facade with a fail-open, vendor-neutral API."""

from __future__ import annotations

import hashlib
import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Mapping

from app.core.logging_system.context import get_log_context
from app.core.logging_system.redaction import is_sensitive_key

_TRACEPARENT_RE = re.compile(r"^(?P<version>[0-9a-f]{2})-(?P<trace>[0-9a-f]{32})-(?P<span>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$")
_FORBIDDEN_ATTRIBUTE_PARTS = ("prompt", "message", "completion", "secret", "password", "token", "authorization", "cookie", "body", "content", "api_key")


@dataclass(frozen=True, slots=True)
class ObservabilitySettings:
    enabled: bool = False
    service_name: str = "testagent-backend"
    otlp_endpoint: str = ""
    sample_rate: float = 1.0


@dataclass(frozen=True, slots=True)
class TraceContext:
    valid: bool
    trace_id: str | None = None
    span_id: str | None = None
    trace_flags: int = 1
    tracestate: str | None = None


_settings = ObservabilitySettings()
_tracer: Any | None = None


def configure_tracing(settings: ObservabilitySettings) -> None:
    """Configure an in-process provider; all failures deliberately degrade to no-op."""
    global _settings, _tracer
    _settings = settings
    _tracer = None
    if not settings.enabled:
        return
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        provider = TracerProvider(resource=Resource.create({"service.name": settings.service_name}))
        if settings.otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
                from opentelemetry.sdk.trace.export import BatchSpanProcessor
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint, insecure=True)))
            except Exception:
                # Exporter availability must never affect business execution.
                pass
        _tracer = provider.get_tracer("testagent.backend")
    except Exception:
        _tracer = None


def get_tracer() -> Any | None:
    """Return the optional OpenTelemetry tracer without exposing setup details."""
    return _tracer


def extract_trace_context(traceparent: str | None, tracestate: str | None = None) -> TraceContext:
    match = _TRACEPARENT_RE.fullmatch((traceparent or "").strip().lower())
    if not match or match.group("trace") == "0" * 32 or match.group("span") == "0" * 16:
        return TraceContext(valid=False)
    return TraceContext(
        valid=True, trace_id=match.group("trace"), span_id=match.group("span"),
        trace_flags=int(match.group("flags"), 16), tracestate=(tracestate or None),
    )


def _safe_attribute(key: str, value: Any) -> bool:
    lowered = key.lower()
    return not is_sensitive_key(key) and not any(part in lowered for part in _FORBIDDEN_ATTRIBUTE_PARTS) and value is not None


class SpanHandle(AbstractContextManager["SpanHandle"]):
    def __init__(self, name: str, attributes: Mapping[str, Any] | None = None, parent: TraceContext | None = None) -> None:
        self.name = name
        self.attributes = dict(attributes or {})
        self.parent = parent
        self._cm: Any | None = None
        self._span: Any | None = None
        self.trace_id: str | None = None
        self.span_id: str | None = None
        self.is_recording = False

    def __enter__(self) -> "SpanHandle":
        if _tracer is None:
            return self
        try:
            context = None
            if self.parent and self.parent.valid:
                from opentelemetry import trace
                from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
                span_context = SpanContext(
                    trace_id=int(self.parent.trace_id or "0", 16), span_id=int(self.parent.span_id or "0", 16),
                    is_remote=True, trace_flags=TraceFlags(self.parent.trace_flags), trace_state=None,
                )
                context = trace.set_span_in_context(NonRecordingSpan(span_context))
            self._cm = _tracer.start_as_current_span(self.name, context=context)
            self._span = self._cm.__enter__()
            span_context = self._span.get_span_context()
            self.trace_id = f"{span_context.trace_id:032x}"
            self.span_id = f"{span_context.span_id:016x}"
            self.is_recording = bool(self._span.is_recording())
            for key, value in self.attributes.items():
                self.set_attribute(key, value)
        except Exception:
            self._cm = None
            self._span = None
            self.trace_id = None
            self.span_id = None
            self.is_recording = False
        return self

    def set_attribute(self, key: str, value: Any) -> None:
        if self._span is not None and _safe_attribute(key, value):
            try:
                self._span.set_attribute(key, value)
            except Exception:
                pass

    def record_exception(self, exc: BaseException) -> None:
        if self._span is not None:
            try:
                from opentelemetry.trace.status import Status, StatusCode
                self._span.record_exception(exc)
                self._span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            except Exception:
                pass

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> bool | None:
        if exc is not None:
            self.record_exception(exc)
        if self._cm is not None:
            try:
                return self._cm.__exit__(exc_type, exc, traceback)
            except Exception:
                return None
        return None


def start_span(name: str, attributes: Mapping[str, Any] | None = None, *, parent: TraceContext | None = None) -> SpanHandle:
    return SpanHandle(name, attributes, parent)


def _active_context() -> Any | None:
    try:
        from opentelemetry import trace
        context = trace.get_current_span().get_span_context()
        return context if context and context.is_valid else None
    except Exception:
        return None


def current_trace_id() -> str | None:
    context = _active_context()
    if context is not None:
        return f"{context.trace_id:032x}"
    value = get_log_context().get("trace_id")
    return value if isinstance(value, str) else None


def current_span_id() -> str | None:
    context = _active_context()
    return f"{context.span_id:016x}" if context is not None else None


def inject_trace_context() -> dict[str, str]:
    trace_id, span_id = current_trace_id(), current_span_id()
    if not trace_id or not span_id:
        return {}
    return {"traceparent": f"00-{trace_id}-{span_id}-01"}


def invalid_trace_value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]
