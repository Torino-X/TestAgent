"""Regression tests for the Windows-safe development launcher."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


RUN_DEV_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_dev.py"


def _load_run_dev_module():
    spec = importlib.util.spec_from_file_location("testagent_run_dev", RUN_DEV_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_sets_selector_policy_before_calling_uvicorn(monkeypatch) -> None:
    run_dev = _load_run_dev_module()
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(run_dev.sys, "platform", "win32")
    monkeypatch.setattr(
        run_dev.asyncio,
        "set_event_loop_policy",
        lambda policy: calls.append(("policy", policy)),
    )
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(
            run=lambda app, **kwargs: calls.append(("uvicorn", (app, kwargs)))
        ),
    )

    run_dev.main(["--host", "127.0.0.1", "--port", "8003", "--log-level", "debug"])

    assert calls[0][0] == "policy"
    assert calls[1] == (
        "uvicorn",
        (
            "app.main:app",
            {
                "host": "127.0.0.1",
                "port": 8003,
                "reload": False,
                "log_level": "debug",
                "loop": "app.uvicorn_loop:selector_loop_factory",
            },
        ),
    )


def test_windows_launcher_rejects_reload(monkeypatch) -> None:
    run_dev = _load_run_dev_module()
    monkeypatch.setattr(run_dev.sys, "platform", "win32")

    with pytest.raises(SystemExit, match="2"):
        run_dev.main(["--reload"])
