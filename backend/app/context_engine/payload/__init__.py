"""Payload 层：PayloadStorageService + FileSystemPayloadBackend + PayloadReader。"""

from app.context_engine.payload.payload_storage import (
    FileSystemPayloadBackend,
    OSSPayloadBackend,
    PayloadBackend,
    PayloadStorageService,
)
from app.context_engine.payload.reader import PayloadReader
from app.context_engine.payload.factory import (
    build_external_payload_backends,
    build_payload_backend,
    filesystem_payload_root,
)

__all__ = [
    "FileSystemPayloadBackend",
    "OSSPayloadBackend",
    "PayloadBackend",
    "PayloadStorageService",
    "PayloadReader",
    "build_external_payload_backends",
    "build_payload_backend",
    "filesystem_payload_root",
]
# auto-appended module-level note: payload 子包: payload 读写入口。
