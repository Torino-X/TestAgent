"""Alibaba Cloud OSS storage for durable TestAgent file bodies.

Database ``storage_path`` values contain opaque OSS object keys; clients never
receive bucket URLs or credentials.  Local files are used only as short-lived
processing scratch files through :meth:`stage_file`.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path, PurePosixPath
from typing import AsyncGenerator, Callable

from app.core.config import get_settings
from app.storage.base import FileStorageService, StoredFile
from app.utils.ids import safe_filename


class OSSStorageConfigurationError(RuntimeError):
    """Raised when durable object storage is used before OSS is configured."""


class OSSFileStorageService(FileStorageService):
    """Private OSS bucket adapter with async wrappers around the OSS SDK."""

    storage_type = "oss"

    def __init__(self, *, bucket_factory: Callable[[], object] | None = None) -> None:
        self._bucket_factory = bucket_factory
        self._bucket: object | None = None

    def _get_bucket(self):
        if self._bucket is not None:
            return self._bucket
        if self._bucket_factory is not None:
            self._bucket = self._bucket_factory()
            return self._bucket

        settings = get_settings()
        if not settings.oss_is_configured:
            raise OSSStorageConfigurationError(
                "OSS is not configured. Set OSS_ENDPOINT, OSS_ACCESS_KEY_ID, "
                "OSS_ACCESS_KEY_SECRET, and OSS_BUCKET_NAME in backend/.env."
            )
        try:
            import oss2
        except ImportError as exc:  # pragma: no cover - exercised in deployment
            raise OSSStorageConfigurationError(
                "The oss2 package is required for OSS storage; install backend dependencies."
            ) from exc
        self._bucket = oss2.Bucket(
            oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret),
            settings.oss_endpoint,
            settings.oss_bucket_name,
        )
        return self._bucket

    @staticmethod
    def _validate_key(storage_path: str) -> str:
        path = PurePosixPath(storage_path)
        if not storage_path or path.is_absolute() or ".." in path.parts:
            raise FileNotFoundError(storage_path)
        return path.as_posix()

    @staticmethod
    def _prefix() -> str:
        prefix = get_settings().oss_prefix.strip("/ ")
        return prefix or "testagent"

    def _new_key(self, category: str, *parts: str, file_name: str) -> str:
        safe_name = safe_filename(file_name) or "file"
        return "/".join(
            [self._prefix(), category, *(str(part) for part in parts), f"{uuid.uuid4().hex}_{safe_name}"]
        )

    def put_bytes_sync(self, storage_path: str, content: bytes) -> None:
        key = self._validate_key(storage_path)
        result = self._get_bucket().put_object(key, content)
        if getattr(result, "status", 200) not in {200, 201, 204}:
            raise OSError(f"OSS upload failed with status {getattr(result, 'status', 'unknown')}")

    def read_bytes_sync(self, storage_path: str) -> bytes:
        key = self._validate_key(storage_path)
        try:
            return self._get_bucket().get_object(key).read()
        except Exception as exc:  # SDK error classes are optional at import time
            if "NoSuchKey" in type(exc).__name__ or "NoSuchObject" in type(exc).__name__:
                raise FileNotFoundError(storage_path) from exc
            raise

    def exists_sync(self, storage_path: str) -> bool:
        key = self._validate_key(storage_path)
        try:
            return bool(self._get_bucket().object_exists(key))
        except Exception as exc:
            if "NoSuchKey" in type(exc).__name__ or "NoSuchObject" in type(exc).__name__:
                return False
            raise

    def delete_file_sync(self, storage_path: str) -> None:
        key = self._validate_key(storage_path)
        self._get_bucket().delete_object(key)

    async def save_upload(
        self, content: bytes, original_name: str, user_id: str, conversation_id: str
    ) -> StoredFile:
        key = self._new_key("uploads", user_id, conversation_id, file_name=original_name)
        await asyncio.to_thread(self.put_bytes_sync, key, content)
        return StoredFile(storage_path=key, file_name=original_name, file_size=len(content))

    async def save_artifact(
        self, content: bytes, file_name: str, user_id: str, task_id: str
    ) -> StoredFile:
        key = self._new_key("artifacts", user_id, task_id, file_name=file_name)
        await asyncio.to_thread(self.put_bytes_sync, key, content)
        return StoredFile(storage_path=key, file_name=file_name, file_size=len(content))

    async def save_preview(
        self, content: bytes, file_name: str, user_id: str, item_id: str
    ) -> StoredFile:
        """Store a private PDF derivative outside normal library prefixes."""
        key = self._new_key("previews", user_id, item_id, file_name=file_name)
        await asyncio.to_thread(self.put_bytes_sync, key, content)
        return StoredFile(storage_path=key, file_name=file_name, file_size=len(content))

    async def save_template(
        self,
        content: bytes,
        file_name: str,
        owner_id: str,
        template_id: str,
        version_no: int,
    ) -> StoredFile:
        """Persist an immutable canonical template version in its own namespace."""
        key = self._new_key(
            "templates",
            owner_id,
            template_id,
            f"v{version_no}",
            file_name=file_name,
        )
        await asyncio.to_thread(self.put_bytes_sync, key, content)
        return StoredFile(storage_path=key, file_name=file_name, file_size=len(content))

    async def read_bytes(self, storage_path: str) -> bytes:
        return await asyncio.to_thread(self.read_bytes_sync, storage_path)

    async def exists(self, storage_path: str) -> bool:
        return await asyncio.to_thread(self.exists_sync, storage_path)

    async def open_file(self, storage_path: str) -> AsyncGenerator[bytes, None]:
        """Stream a private OSS object without materializing it in API memory."""
        key = self._validate_key(storage_path)
        try:
            response = await asyncio.to_thread(self._get_bucket().get_object, key)
        except Exception as exc:
            if "NoSuchKey" in type(exc).__name__ or "NoSuchObject" in type(exc).__name__:
                raise FileNotFoundError(storage_path) from exc
            raise
        try:
            while chunk := await asyncio.to_thread(response.read, 256 * 1024):
                yield chunk
        finally:
            with suppress(Exception):
                await asyncio.to_thread(response.close)

    async def delete_file(self, storage_path: str) -> None:
        await asyncio.to_thread(self.delete_file_sync, storage_path)

    @asynccontextmanager
    async def stage_file(self, storage_path: str, *, suffix: str = ""):
        """Materialize an object only for the duration of a path-only consumer."""
        content = await self.read_bytes(storage_path)
        fd, name = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            yield Path(name)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass


object_storage = OSSFileStorageService()
