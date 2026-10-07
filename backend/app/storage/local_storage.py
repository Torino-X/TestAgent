"""Local filesystem storage implementation."""

from __future__ import annotations

import os
import asyncio
from pathlib import Path
from typing import AsyncGenerator

from app.core.config import get_settings
from app.services.atomic_file_writer import atomic_write_bytes_async
from app.storage.base import FileStorageService, StoredFile
from app.utils.file_utils import ensure_dir
from app.utils.ids import safe_filename

settings = get_settings()


class LocalFileStorageService(FileStorageService):
    """Store files on the local filesystem under ./data/.

    The on-disk layout mirrors the isolation scheme:
        data/uploads/{user_id}/{conversation_id}/
        data/artifacts/{user_id}/{task_id}/
        data/temp/
    """

    def __init__(self) -> None:
        self._base = Path(settings.local_storage_path).resolve()
        ensure_dir(self._base / "uploads")
        ensure_dir(self._base / "artifacts")
        ensure_dir(self._base / "templates")
        ensure_dir(self._base / "temp")

    async def save_upload(
        self, content: bytes, original_name: str, user_id: str, conversation_id: str
    ) -> StoredFile:
        dir_path = self._base / "uploads" / user_id / conversation_id
        ensure_dir(dir_path)
        stored_name = safe_filename(original_name)
        full_path = dir_path / stored_name
        full_path.write_bytes(content)
        return StoredFile(
            storage_path=str(full_path.relative_to(self._base)),
            file_name=original_name,
            file_size=len(content),
        )

    async def save_artifact(
        self, content: bytes, file_name: str, user_id: str, task_id: str
    ) -> StoredFile:
        dir_path = self._base / "artifacts" / user_id / task_id
        ensure_dir(dir_path)
        stored_name = safe_filename(file_name)
        full_path = dir_path / stored_name
        full_path.write_bytes(content)
        return StoredFile(
            storage_path=str(full_path.relative_to(self._base)),
            file_name=file_name,
            file_size=len(content),
        )

    async def save_preview(
        self, content: bytes, file_name: str, user_id: str, item_id: str
    ) -> StoredFile:
        dir_path = self._base / "previews" / user_id / item_id
        ensure_dir(dir_path)
        stored_name = safe_filename(file_name)
        full_path = dir_path / stored_name
        full_path.write_bytes(content)
        return StoredFile(
            storage_path=str(full_path.relative_to(self._base)),
            file_name=file_name,
            file_size=len(content),
        )

    async def save_template(
        self,
        content: bytes,
        file_name: str,
        owner_id: str,
        template_id: str,
        version_no: int,
    ) -> StoredFile:
        dir_path = self._base / "templates" / owner_id / template_id / f"v{version_no}"
        ensure_dir(dir_path)
        stored_name = safe_filename(file_name)
        full_path = dir_path / stored_name
        await atomic_write_bytes_async(full_path, content)
        return StoredFile(
            storage_path=str(full_path.relative_to(self._base)),
            file_name=file_name,
            file_size=len(content),
        )

    async def read_bytes(self, storage_path: str) -> bytes:
        full_path = self._base / storage_path
        if not full_path.is_file():
            raise FileNotFoundError(storage_path)
        return await asyncio.to_thread(full_path.read_bytes)

    async def exists(self, storage_path: str) -> bool:
        return await asyncio.to_thread((self._base / storage_path).is_file)

    async def open_file(self, storage_path: str) -> AsyncGenerator[bytes, None]:
        full_path = self._base / storage_path
        if not full_path.exists():
            raise FileNotFoundError(storage_path)
        with full_path.open("rb") as handle:
            while chunk := await asyncio.to_thread(handle.read, 256 * 1024):
                yield chunk

    async def delete_file(self, storage_path: str) -> None:
        full_path = self._base / storage_path
        try:
            os.remove(full_path)
        except FileNotFoundError:
            pass


# Module-level singleton
local_storage = LocalFileStorageService()
# local_storage:本地文件系统存储实现(根目录由配置 LOCAL_STORAGE_ROOT 控制);用于开发与单机部署。
