"""Resolve non-model Context Engine runtime configuration safely.

Pydantic ``Settings`` reads ``backend/.env`` without exporting undeclared keys
to ``os.environ``.  Context Engine's external provider factories are invoked
outside Settings, so they need the same effective view.  Process environment
values remain the explicit operator override; this module never mutates the
process environment and callers must never log returned secret values.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values


_DOTENV_PATH = Path(__file__).resolve().parents[3] / ".env"


@lru_cache
def _read_dotenv_runtime_values() -> dict[str, str]:
    try:
        values = dotenv_values(_DOTENV_PATH)
    except OSError:
        return {}
    return {
        str(key): str(value)
        for key, value in values.items()
        if key is not None and value is not None
    }


def get_context_engine_runtime_env() -> dict[str, str]:
    """Return `.env` values overlaid by explicit process-environment values."""
    resolved = dict(_read_dotenv_runtime_values())
    resolved.update({str(key): str(value) for key, value in os.environ.items()})
    return resolved


__all__ = ["get_context_engine_runtime_env"]
