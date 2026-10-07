"""Production Context Payload backend construction.

The persisted ``storage_backend`` value is authoritative.  ``inline`` rows do
not have an external blob; ``fs`` exists only for historical compatibility and
uses one deterministic configured root; new durable rows use private OSS.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.context_engine.payload.payload_storage import (
    FileSystemPayloadBackend,
    OSSPayloadBackend,
    PayloadBackend,
)


def filesystem_payload_root() -> Path:
    configured = os.environ.get("CONTEXT_PAYLOAD_FS_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parents[3] / "data" / "ce_payloads").resolve()


def build_payload_backend(
    storage_backend: str,
    *,
    fs_root: str | Path | None = None,
) -> PayloadBackend | None:
    normalized = (storage_backend or "").strip().lower()
    if normalized == "inline":
        return None
    if normalized == "fs":
        return FileSystemPayloadBackend(fs_root or filesystem_payload_root())
    if normalized == "oss":
        return OSSPayloadBackend()
    raise ValueError(f"unsupported context payload backend: {storage_backend!r}")


def build_external_payload_backends() -> dict[str, PayloadBackend]:
    return {
        "fs": build_payload_backend("fs"),
        "oss": build_payload_backend("oss"),
    }


__all__ = [
    "build_external_payload_backends",
    "build_payload_backend",
    "filesystem_payload_root",
]
