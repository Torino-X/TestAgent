"""Stable, safe error classification and fingerprinting for Logging V2."""

from __future__ import annotations

import hashlib
import re
import traceback
from typing import Any

_FRAME_RE = re.compile(r'File "(?P<path>[^"]+)", line \d+(?:, in (?P<func>[^\n]+))?')


def classify_error(event: str, exc_info: tuple[Any, Any, Any] | None, fields: dict[str, Any]) -> dict[str, Any]:
    """Return taxonomy fields without using an exception's free-form message."""
    domain = str(fields.get("error_domain") or event.split(".", 1)[0] or "application")
    exception_type = str(fields.get("error_type") or "")
    if exc_info and exc_info[0]:
        exception_type = exc_info[0].__name__
    lowered = exception_type.lower()
    kind = str(fields.get("error_kind") or "")
    if not kind:
        if "timeout" in lowered:
            kind = "timeout"
        elif "connection" in lowered or "connect" in lowered:
            kind = "connection"
        elif "permission" in lowered or "auth" in lowered:
            kind = "permission"
        elif "validation" in lowered or "valueerror" in lowered:
            kind = "validation"
        elif "notfound" in lowered or "keyerror" in lowered:
            kind = "not_found"
        else:
            kind = "internal"
    return {
        "error_domain": domain,
        "error_kind": kind,
        "error_type": exception_type or "Exception",
        "error_code": fields.get("error_code"),
        "retryable": bool(fields.get("retryable", kind in {"timeout", "connection", "unavailable", "rate_limit"})),
    }


def build_error_fingerprint(
    *, event: str, error_domain: str, error_kind: str, exception_type: str,
    error_code: str | None = None, stacktrace: str | None = None,
) -> str:
    """Hash stable error identity; never include exception messages or IDs."""
    frames: list[str] = []
    for match in _FRAME_RE.finditer(stacktrace or ""):
        path = match.group("path").replace("\\", "/").split("/")[-3:]
        frames.append(f"{'/'.join(path)}:{match.group('func') or '?'}")
        if len(frames) == 5:
            break
    signature = "|".join((event, error_domain, error_kind, exception_type, error_code or "", *frames))
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]


def fingerprint_from_record(event: str, record: Any, fields: dict[str, Any]) -> tuple[dict[str, Any], str]:
    taxonomy = classify_error(event, record.exc_info, fields)
    stacktrace = ""
    if record.exc_info:
        stacktrace = "".join(traceback.format_exception(*record.exc_info))
    return taxonomy, build_error_fingerprint(
        stacktrace=stacktrace,
        event=event,
        error_domain=str(taxonomy["error_domain"]),
        error_kind=str(taxonomy["error_kind"]),
        exception_type=str(taxonomy["error_type"]),
        error_code=str(taxonomy["error_code"]) if taxonomy.get("error_code") else None,
    )
