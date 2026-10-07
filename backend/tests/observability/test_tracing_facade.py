from __future__ import annotations

import json
import logging

from app.core.logging_system.formatter import JsonFormatter
from app.core.observability import ObservabilitySettings, configure_tracing, current_span_id, current_trace_id, extract_trace_context, start_span


def test_traceparent_parsing_and_disabled_noop():
    parsed = extract_trace_context("00-0123456789abcdef0123456789abcdef-0123456789abcdef-01", "vendor=value")
    assert parsed.valid is True
    assert parsed.trace_id == "0123456789abcdef0123456789abcdef"
    assert extract_trace_context("bad-traceparent").valid is False

    configure_tracing(ObservabilitySettings(enabled=False))
    with start_span("test.noop") as span:
        assert span.is_recording is False
        assert current_span_id() is None


def test_active_span_is_injected_into_structured_logs():
    configure_tracing(ObservabilitySettings(enabled=True, service_name="testagent-test"))
    with start_span("test.parent", attributes={"safe": "value"}) as parent:
        assert len(current_trace_id() or "") == 32
        assert len(current_span_id() or "") == 16
        with start_span("test.child") as child:
            assert child.trace_id == parent.trace_id
            formatter = JsonFormatter(environment="test")
            record = logging.LogRecord("testagent.application", logging.INFO, __file__, 1, "trace log", (), None)
            record.event = "application.startup.completed"
            payload = json.loads(formatter.format(record))
            assert payload["trace_id"] == child.trace_id
            assert payload["span_id"] == child.span_id
