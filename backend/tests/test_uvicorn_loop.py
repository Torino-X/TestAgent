"""Verify the Uvicorn loop factory used by the Windows launcher."""

from __future__ import annotations

import asyncio

import uvicorn


def test_selector_loop_factory_resolves_through_uvicorn_config() -> None:
    config = uvicorn.Config(
        "app.main:app",
        loop="app.uvicorn_loop:selector_loop_factory",
    )

    loop_factory = config.get_loop_factory()

    assert loop_factory is not None
    loop = loop_factory()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
        assert not isinstance(loop, asyncio.ProactorEventLoop)
    finally:
        loop.close()
