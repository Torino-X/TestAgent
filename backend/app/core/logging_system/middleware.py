"""HTTP correlation and lifecycle logging without changing response bodies.

Implemented as a *pure ASGI* middleware on purpose.  ``BaseHTTPMiddleware``
spawns the inner app inside an anyio task group whose contextvar copy is
isolated from the rest of the request lifecycle.  That breaks two
production-critical behaviours:

* ``request_id`` / ``trace_id`` bound by the route handler never reach
  middleware-owned logging (e.g. ``http.request.failed``).
* More importantly, ``BaseHTTPMiddleware``'s ``finally`` runs *before*
  Starlette's ``ServerErrorMiddleware`` invokes the user's global
  exception handler — so the global fallback's ``logger.exception``
  loses the correlation context.

The pure ASGI form binds the context once for the entire ASGI request,
stamps ``X-Request-ID`` on every response that flows through us (2xx,
4xx, 422, validation, HTTPException), and writes the same correlation
values to ``scope["state"]`` so ``ServerErrorMiddleware``'s fallback
handler (which runs *outside* this middleware's contextvar block) can
rebind them before logging.

Note: ``ServerErrorMiddleware`` sends its fallback response via the
*original* ``send`` (bypassing our wrapper), so the 500 response from
an unhandled exception must stamp ``X-Request-ID`` itself — see the
``fallback_handler`` in ``app/main.py``.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

from app.core.observability import extract_trace_context, invalid_trace_value_hash, start_span

from .context import logging_context
from .events import LogEvent, log_event

logger = logging.getLogger("testagent.http")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


def _incoming_request_id(scope: dict[str, Any]) -> str:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            try:
                return value.decode("latin-1")
            except UnicodeDecodeError:
                return ""
    return ""


def _incoming_header(scope: dict[str, Any], name: bytes) -> str:
    for header_name, value in scope.get("headers", []):
        if header_name.lower() == name:
            try:
                return value.decode("latin-1")
            except UnicodeDecodeError:
                return ""
    return ""


def _new_request_id(scope: dict[str, Any]) -> str:
    incoming = _incoming_request_id(scope)
    return incoming if _VALID_REQUEST_ID.fullmatch(incoming) else f"req_{uuid.uuid4().hex}"


def get_request_correlation(request: Any) -> tuple[str | None, str | None]:
    """Read correlation fields stamped by ``RequestLoggingMiddleware``.

    The Starlette ``Request.scope["state"]`` survives across all
    middleware layers (including ``ServerErrorMiddleware``), so handlers
    that run in a fresh contextvar copy can rebind the same values
    before logging.
    """
    scope = getattr(request, "scope", None) or {}
    state = scope.get("state") if isinstance(scope, dict) else None
    if not state:
        return None, None
    request_id = state.get("request_id")
    trace_id = state.get("trace_id")
    return (
        request_id if isinstance(request_id, str) else None,
        trace_id if isinstance(trace_id, str) else None,
    )


class RequestLoggingMiddleware:
    """Pure ASGI middleware: correlation + lifecycle + X-Request-ID stamping."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _new_request_id(scope)
        incoming_traceparent = _incoming_header(scope, b"traceparent")
        incoming_tracestate = _incoming_header(scope, b"tracestate")
        parent_context = extract_trace_context(incoming_traceparent, incoming_tracestate)
        trace_id = parent_context.trace_id if parent_context.valid else uuid.uuid4().hex
        span_id = uuid.uuid4().hex[:16]
        route = scope.get("path", "/")
        method = scope.get("method", "?")
        started = time.perf_counter()

        # Persist correlation on ``scope["state"]`` so exception
        # handlers that run outside this middleware's contextvar block
        # (e.g. ``ServerErrorMiddleware``'s fallback) can re-bind it.
        state = scope.setdefault("state", {})
        if not isinstance(state, dict):
            state = {}
            scope["state"] = state
        response_status = 500
        response_started = False
        response_traceparent = f"00-{trace_id}-{span_id}-01"

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal response_started, response_status
            if message["type"] == "http.response.start":
                response_started = True
                response_status = message["status"]
                # Stamp X-Request-ID on every response that flows
                # through us (2xx / 4xx / 422 / HTTPException).  Strip
                # any downstream copy first so we are the single
                # source of truth.  ``ServerErrorMiddleware``'s own
                # fallback response bypasses this wrapper and stamps
                # the header itself (see ``app.main.fallback_handler``).
                headers = [
                    item
                    for item in (message.get("headers") or [])
                    if item[0].lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                headers = [item for item in headers if item[0].lower() != b"traceparent"]
                headers.append((b"traceparent", response_traceparent.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        with start_span(
            f"HTTP {method} {route}",
            {"http.request.method": method, "http.route": route, "testagent.request_id": request_id},
            parent=parent_context if parent_context.valid else None,
        ) as http_span:
            trace_id = http_span.trace_id or trace_id
            span_id = http_span.span_id or span_id
            response_traceparent = f"00-{trace_id}-{span_id}-01"
            state["request_id"] = request_id
            state["trace_id"] = trace_id
            state["span_id"] = span_id
            state["traceparent"] = response_traceparent
            state["tracestate"] = parent_context.tracestate
            with logging_context(request_id=request_id, trace_id=trace_id, span_id=span_id):
                if incoming_traceparent and not parent_context.valid:
                    log_event(
                        logger, logging.WARNING, LogEvent.SECURITY_INVALID_TRACE_CONTEXT,
                        "Invalid trace context replaced", rule_id="invalid_traceparent",
                        length=len(incoming_traceparent), value_hash=invalid_trace_value_hash(incoming_traceparent),
                    )
                log_event(
                    logger, logging.DEBUG, LogEvent.HTTP_REQUEST_STARTED, "HTTP request started",
                    method=method, route=route,
                )
                try:
                    await self.app(scope, receive, send_wrapper)
                except Exception:
                # Inner app did not convert the exception into a
                # response (FastAPI registers ``Exception`` handlers
                # on ``ServerErrorMiddleware``, which is OUTSIDE us).
                # Log FAILED here so the record carries our
                # correlation, then re-raise so
                # ``ServerErrorMiddleware`` can run the fallback.
                    duration_ms = round((time.perf_counter() - started) * 1000, 2)
                    log_event(
                        logger, logging.WARNING, LogEvent.HTTP_REQUEST_FAILED, "HTTP request failed",
                        method=method, route=route, duration_ms=duration_ms,
                    )
                    raise
                else:
                    http_span.set_attribute("http.response.status_code", response_status)
                    duration_ms = round((time.perf_counter() - started) * 1000, 2)
                    level = logging.DEBUG if route.startswith("/health") and response_status < 300 else logging.INFO
                    log_event(
                        logger, level, LogEvent.HTTP_REQUEST_COMPLETED, "HTTP request completed",
                        method=method, route=route, status_code=response_status, duration_ms=duration_ms,
                    )
