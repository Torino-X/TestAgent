"""Task-local correlation context backed by :mod:`contextvars`."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

_context: ContextVar[dict[str, Any]] = ContextVar("testagent_log_context", default={})


def bind_log_context(**values: Any) -> None:
    """Merge non-null correlation values into the current async context."""
    merged = dict(_context.get())
    merged.update({key: value for key, value in values.items() if value is not None})
    _context.set(merged)


def unbind_log_context(*keys: str) -> None:
    merged = dict(_context.get())
    for key in keys:
        merged.pop(key, None)
    _context.set(merged)


def clear_log_context() -> None:
    _context.set({})


def get_log_context() -> dict[str, Any]:
    return dict(_context.get())


@contextmanager
def logging_context(**values: Any) -> Iterator[None]:
    previous = _context.set({**_context.get(), **{k: v for k, v in values.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(previous)
