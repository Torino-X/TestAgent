"""Abstract file storage interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncGenerator


class StoredFile:
    """Metadata returned after a successful save."""

    def __init__(self, storage_path: str, file_name: str, file_size: int) -> None:
        self.storage_path = storage_path
        self.file_name = file_name
        self.file_size = file_size


class FileStorageService(ABC):
    """Abstract file storage — swap LocalFileStorageService with MinIO, S3, etc."""

    @abstractmethod
    async def save_upload(
        self, content: bytes, original_name: str, user_id: str, conversation_id: str
    ) -> StoredFile: ...

    @abstractmethod
    async def save_artifact(
        self, content: bytes, file_name: str, user_id: str, task_id: str
    ) -> StoredFile: ...

    @abstractmethod
    async def save_preview(
        self, content: bytes, file_name: str, user_id: str, item_id: str
    ) -> StoredFile: ...

    @abstractmethod
    async def save_template(
        self,
        content: bytes,
        file_name: str,
        owner_id: str,
        template_id: str,
        version_no: int,
    ) -> StoredFile: ...

    @abstractmethod
    async def read_bytes(self, storage_path: str) -> bytes: ...

    @abstractmethod
    async def exists(self, storage_path: str) -> bool: ...

    @abstractmethod
    async def open_file(self, storage_path: str) -> AsyncGenerator[bytes, None]: ...

    @abstractmethod
    async def delete_file(self, storage_path: str) -> None: ...
# storage.base:Storage protocol(put / get / delete / exists / url);新写适配器必须实现全部方法。
