"""Central, output-boundary redaction for structured application logs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

MASK = "***"
_SENSITIVE_KEYS = frozenset({
    "password", "passwd", "pwd", "secret", "client_secret", "api_key", "apikey",
    "access_key", "access_key_secret", "token", "access_token", "refresh_token",
    "authorization", "cookie", "set_cookie", "jwt", "private_key", "dsn",
    "database_url", "postgres_url", "redis_url", "connection_string",
})
_KEY_VALUE = re.compile(r"(?i)(\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key(?:[_-]?secret)?|token|authorization|cookie|jwt|private[_-]?key|dsn|database[_-]?url|postgres[_-]?url|redis[_-]?url|connection[_-]?string)\b\s*[:=]\s*)([^,;\s&\]\)}]+)")
_BEARER = re.compile(r"(?i)(\bBearer\s+)([^\s,;]+)")
_URL_CREDENTIALS = re.compile(r"(?i)(\b(?:redis|mysql|postgres(?:ql)?)(?:\+[^:]+)?://[^@\s/:]*:)([^@\s/]+)(@)")
_QUERY_SECRET = re.compile(r"(?i)([?&](?:token|key|secret|password|api_key|apikey)=)([^&#\s]+)")


def is_sensitive_key(value: object) -> bool:
    normalised = str(value).strip().lower().replace("-", "_")
    return normalised in _SENSITIVE_KEYS


def redact_text(value: object) -> str:
    text = str(value)
    text = _URL_CREDENTIALS.sub(r"\1***\3", text)
    text = _BEARER.sub(r"\1***", text)
    text = _KEY_VALUE.sub(r"\1***", text)
    return _QUERY_SECRET.sub(r"\1***", text)


def redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): MASK if is_sensitive_key(key) else redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, set):
        return [redact_value(item) for item in value]
    if isinstance(value, BaseException):
        return redact_text(value)
    if isinstance(value, str):
        return redact_text(value)
    return value
