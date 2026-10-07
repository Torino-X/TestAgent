"""Event-loop factories used by the local Uvicorn launcher."""

from __future__ import annotations

import asyncio


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Create the Windows loop implementation supported by async Psycopg."""
    return asyncio.SelectorEventLoop()
