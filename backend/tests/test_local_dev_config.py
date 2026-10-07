from __future__ import annotations

import json
from pathlib import Path

from app.core.config import Settings


_LOOPBACK_DEV_ORIGINS = {"http://localhost:5318", "http://127.0.0.1:5318"}


def test_local_vite_port_is_allowed_by_default_and_development_env():
    """A fixed Vite origin must pass CORS before the browser sends login POST."""

    assert _LOOPBACK_DEV_ORIGINS <= set(Settings.model_fields["cors_origins"].default)

    backend_root = Path(__file__).resolve().parents[1]
    env_line = next(
        line for line in (backend_root / ".env").read_text(encoding="utf-8").splitlines() if line.startswith("CORS_ORIGINS=")
    )
    assert _LOOPBACK_DEV_ORIGINS <= set(json.loads(env_line.removeprefix("CORS_ORIGINS=")))


def test_frontend_dev_server_uses_the_cors_authorized_loopback_origin():
    frontend_package = Path(__file__).resolve().parents[2] / "frontend" / "package.json"
    dev_script = json.loads(frontend_package.read_text(encoding="utf-8"))["scripts"]["dev"]

    assert "--host 127.0.0.1" in dev_script
    assert "--port 5318" in dev_script
    assert "--strictPort" in dev_script
