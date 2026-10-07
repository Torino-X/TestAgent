"""PayloadStorageService — Payload 外置 + 基础读取。

CE-02 WP-5：
- ``put`` → 服务端生成 key（UUID，不接受客户端 key）+ 原子写（临时文件 +
  rename）+ SHA-256 + size + metadata；返回 ``ContextPayloadRef``（不含
  storage_key / 路径）。
- ``open`` → owner-scope AsyncIterator[bytes]。
- ``delete`` → owner-scope，引用检查。
- **安全加固**：路径 canonicalization（解析后必须仍位于 backend root 内）、
  禁止 ``..`` traversal、禁止 symlink escape、SHA-256 write/read 双向校验、
  size limit、原子写、DB/file 补偿、expiry 清理、orphan cleanup。
- 默认 storage backend：文件系统 ``data/ce_payloads/``（可配置），加 owner
  子目录隔离；**storage key/path 永不进入 API/State/Prompt/Log**。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Protocol, runtime_checkable

from app.context_engine.errors import ContextEngineStage, raise_engine_error
from app.context_engine.models.payload import ContextPayloadRef, PayloadStoreCommand
from app.context_engine.models.value_objects import Digest


@runtime_checkable
class PayloadBackend(Protocol):
    """存储后端接口（文件系统默认实现）。"""

    def exists(self, owner_user_id: int, storage_key: str) -> bool: ...

    def write(self, owner_user_id: int, storage_key: str, data: bytes) -> None: ...

    def read(self, owner_user_id: int, storage_key: str) -> bytes: ...

    def delete(self, owner_user_id: int, storage_key: str) -> bool: ...

    def iterate_orphans(self) -> list[tuple[int, str]]: ...


class FileSystemPayloadBackend:
    """文件系统后端：``<root>/<owner>/<storage_key>``。

    - 原子写：临时文件 + rename；
    - canonicalization + traversal + symlink escape 防护；
    - owner 子目录隔离。
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _owner_dir(self, owner_user_id: int) -> Path:
        return self._root / str(owner_user_id)

    def _path_for(self, owner_user_id: int, storage_key: str) -> Path:
        owner_dir = self._owner_dir(owner_user_id)
        # 禁止 `..` traversal 段
        if ".." in Path(storage_key).parts or storage_key.startswith("."):
            raise_engine_error(
                code="context.payload.path_traversal",
                detail="payload storage key 非法（traversal）",
                stage=ContextEngineStage.PAYLOAD,
                retryable=False,
                recoverable=False,
            )
        target = (owner_dir / storage_key).resolve()
        # canonicalization：必须仍位于 root 内
        if not self._is_within_root(target):
            raise_engine_error(
                code="context.payload.path_traversal",
                detail="payload storage path 越界",
                stage=ContextEngineStage.PAYLOAD,
                retryable=False,
                recoverable=False,
            )
        # 禁止 symlink escape：真实路径必须仍在 root 内
        real = Path(os.path.realpath(target))
        if not self._is_within_root(real):
            raise_engine_error(
                code="context.payload.symlink_escape",
                detail="payload storage symlink 逃逸",
                stage=ContextEngineStage.PAYLOAD,
                retryable=False,
                recoverable=False,
            )
        return target

    def _is_within_root(self, path: Path) -> bool:
        try:
            path.relative_to(self._root)
            return True
        except ValueError:
            return False

    def exists(self, owner_user_id: int, storage_key: str) -> bool:
        return self._path_for(owner_user_id, storage_key).is_file()

    def write(self, owner_user_id: int, storage_key: str, data: bytes) -> None:
        owner_dir = self._owner_dir(owner_user_id)
        owner_dir.mkdir(parents=True, exist_ok=True)
        target = self._path_for(owner_user_id, storage_key)
        # 原子写：临时文件 + rename
        tmp = owner_dir / f".{storage_key}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, target)

    def read(self, owner_user_id: int, storage_key: str) -> bytes:
        return self._path_for(owner_user_id, storage_key).read_bytes()

    def delete(self, owner_user_id: int, storage_key: str) -> bool:
        target = self._path_for(owner_user_id, storage_key)
        if target.is_file():
            target.unlink()
            return True
        return False

    def iterate_orphans(self) -> list[tuple[int, str]]:
        """列出文件系统中存在的 (owner, storage_key)，供 orphan cleanup 对比。"""
        result: list[tuple[int, str]] = []
        if not self._root.is_dir():
            return result
        for owner_dir in self._root.iterdir():
            if not owner_dir.is_dir():
                continue
            for child in owner_dir.iterdir():
                if child.is_file() and not child.name.startswith("."):
                    try:
                        result.append((int(owner_dir.name), child.name))
                    except ValueError:
                        continue
        return result


class OSSPayloadBackend:
    """Private OSS implementation for Context Engine payload blobs.

    ``storage_key`` remains the existing UUID stored in the database.  The OSS
    prefix and owner partition are derived here, so neither leaks through any
    API or state payload.
    """

    @staticmethod
    def _object_key(owner_user_id: int, storage_key: str) -> str:
        if not storage_key or ".." in Path(storage_key).parts or storage_key.startswith("."):
            raise_engine_error(
                code="context.payload.path_traversal",
                detail="payload storage key invalid",
                stage=ContextEngineStage.PAYLOAD,
                retryable=False,
                recoverable=False,
            )
        from app.storage.oss_storage import object_storage

        return f"{object_storage._prefix()}/context-payloads/{owner_user_id}/{storage_key}"

    def exists(self, owner_user_id: int, storage_key: str) -> bool:
        from app.storage.oss_storage import object_storage

        return object_storage.exists_sync(self._object_key(owner_user_id, storage_key))

    def write(self, owner_user_id: int, storage_key: str, data: bytes) -> None:
        from app.storage.oss_storage import object_storage

        object_storage.put_bytes_sync(self._object_key(owner_user_id, storage_key), data)

    def read(self, owner_user_id: int, storage_key: str) -> bytes:
        from app.storage.oss_storage import object_storage

        return object_storage.read_bytes_sync(self._object_key(owner_user_id, storage_key))

    def delete(self, owner_user_id: int, storage_key: str) -> bool:
        from app.storage.oss_storage import object_storage

        key = self._object_key(owner_user_id, storage_key)
        if not object_storage.exists_sync(key):
            return False
        object_storage.delete_file_sync(key)
        return True

    def iterate_orphans(
        self,
        *,
        max_keys: int = 10_000,
        page_size: int = 1_000,
    ) -> list[tuple[int, str]]:
        """List payload objects under the OSS prefix as (owner_user_id, storage_key).

        Intended for ``PayloadStorageService.cleanup_orphans``; do not call from
        request hot paths — the operation walks the bucket.
        """
        from app.storage.oss_storage import object_storage

        prefix = (
            f"{object_storage._prefix()}/context-payloads/"
        )
        bucket = object_storage._get_bucket()
        emitted: list[tuple[int, str]] = []
        marker: str | None = None
        while True:
            kwargs: dict = {"prefix": prefix, "max_keys": page_size}
            if marker:
                kwargs["marker"] = marker
            result = bucket.list_objects(**kwargs)
            for obj in getattr(result, "object_list", []) or []:
                key = getattr(obj, "key", "")
                if not key.startswith(prefix):
                    continue
                tail = key[len(prefix):]
                parts = tail.split("/", 1)
                if len(parts) != 2:
                    continue
                owner_raw, storage_key = parts
                try:
                    owner = int(owner_raw)
                except ValueError:
                    continue
                emitted.append((owner, storage_key))
                if len(emitted) >= max_keys:
                    return emitted
            next_marker = getattr(result, "next_marker", None)
            if not next_marker:
                break
            marker = next_marker
        return emitted


class PayloadStorageService:
    """Payload 外置服务。

    ``payload_repository`` 可为 repo 实例或 ``session -> repo`` 工厂。
    工厂形态保证每次 session 使用绑定该 session 的 repo（owner-scope 查询）。
    """

    def __init__(
        self,
        payload_repository,
        *,
        backend: PayloadBackend | None = None,
        storage_backend: str = "fs",
        max_size_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self._payload_repo = payload_repository
        self._payload_repo_is_factory = callable(payload_repository)
        self._backend = backend
        self._storage_backend = storage_backend
        self._max_size_bytes = max_size_bytes

    def _repo_for(self, session):
        if self._payload_repo_is_factory:
            return self._payload_repo(session)
        return self._payload_repo

    async def put(
        self,
        command: PayloadStoreCommand,
        *,
        session_factory,
        storage_backend: str | None = None,
    ) -> ContextPayloadRef:
        """服务端生成 key（UUID）+ 原子写 + DB 记录。"""
        if self._backend is None:
            raise_engine_error(
                code="context.payload.backend_missing",
                detail="Payload 后端未配置",
                stage=ContextEngineStage.PAYLOAD,
                retryable=False,
                recoverable=True,
            )

        storage_backend = storage_backend or self._storage_backend
        content = command.content
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        if len(data) > self._max_size_bytes:
            raise_engine_error(
                code="context.payload.too_large",
                detail="Payload 超过 size limit",
                stage=ContextEngineStage.PAYLOAD,
                retryable=False,
                recoverable=False,
            )

        sha256 = Digest.of(data)
        storage_key = str(uuid.uuid4())
        payload_public_id = _gen_public_id()
        size_bytes = len(data)

        # 1. 先写文件（原子）
        self._backend.write(command.user_id, storage_key, data)

        # 2. 再写 DB；DB 失败 → 补偿清理文件
        try:
            async with session_factory() as session:
                from app.models.context_engine import ContextPayload

                row = ContextPayload(
                    public_id=payload_public_id,
                    user_id=command.user_id,
                    source_type="context_engine",
                    payload_type="context_payload",
                    storage_backend=storage_backend,
                    storage_key=storage_key,
                    storage_key_hash=Digest.of(f"{storage_backend}:{storage_key}"),
                    mime_type=command.media_type,
                    size_bytes=size_bytes,
                    char_count=len(content) if isinstance(content, str) else None,
                    sha256=str(sha256),
                    status="active",
                    metadata_json=command.metadata or None,
                    expires_at=command.expires_at,
                )
                session.add(row)
                await session.flush()
                await session.commit()
        except Exception:
            # DB 失败 → 补偿：删除已写文件，避免孤儿
            self._backend.delete(command.user_id, storage_key)
            raise

        return ContextPayloadRef(
            payload_public_id=payload_public_id,
            storage_backend=storage_backend,
            owner_user_id=command.user_id,
            content_sha256=sha256,
            size_bytes=size_bytes,
            media_type=command.media_type,
            expires_at=command.expires_at,
        )

    async def open(
        self,
        user_id: int,
        payload_public_id: str,
        *,
        session_factory,
    ) -> AsyncIterator[bytes]:
        """owner-scope 读取。"""
        if self._backend is None:
            raise_engine_error(
                code="context.payload.backend_missing",
                detail="Payload 后端未配置",
                stage=ContextEngineStage.PAYLOAD,
                retryable=False,
                recoverable=True,
            )

        async with session_factory() as session:
            row = await self._repo_for(session).get_by_public_id(payload_public_id, user_id)
            if row is None:
                raise_engine_error(
                    code="context.payload.not_found",
                    detail="Payload 不存在或不属于当前用户",
                    stage=ContextEngineStage.PAYLOAD,
                    retryable=False,
                    recoverable=False,
                )
            now = datetime.now(timezone.utc)
            expires_at = getattr(row, "expires_at", None)
            if expires_at is not None:
                comparable_now = (
                    now.replace(tzinfo=None) if expires_at.tzinfo is None else now
                )
            else:
                comparable_now = now
            if (
                getattr(row, "deleted_at", None) is not None
                or getattr(row, "status", None) != "active"
                or (expires_at is not None and expires_at <= comparable_now)
            ):
                raise_engine_error(
                    code="context.payload.gone",
                    detail="Payload 已删除、过期或不可用",
                    stage=ContextEngineStage.PAYLOAD,
                    retryable=False,
                    recoverable=False,
                )
            storage_key = row.storage_key
            sha256 = row.sha256

        data = self._backend.read(user_id, storage_key)
        # SHA-256 read 校验
        actual = Digest.of(data)
        if sha256 and str(actual) != sha256:
            raise_engine_error(
                code="context.payload.checksum_mismatch",
                detail="Payload 内容校验失败",
                stage=ContextEngineStage.PAYLOAD,
                retryable=False,
                recoverable=False,
            )
        yield data

    async def delete(
        self,
        user_id: int,
        payload_public_id: str,
        *,
        session_factory,
    ) -> bool:
        """owner-scope 删除 + 引用检查。"""
        async with session_factory() as session:
            from app.models.context_engine import ContextPayload

            row = await self._repo_for(session).get_by_public_id(payload_public_id, user_id)
            if row is None:
                return False
            storage_key = row.storage_key
            # 软删除
            row.deleted_at = datetime.now(timezone.utc)
            row.status = "deleted"
            await session.flush()
            await session.commit()

        # 文件删除（补偿独立于 DB 事务）
        if self._backend is not None:
            self._backend.delete(user_id, storage_key)
        return True

    # ── 维护 ────────────────────────────────────────────────────────

    async def cleanup_expired(
        self,
        *,
        session_factory,
        now: datetime | None = None,
    ) -> int:
        """expiry 清理：过期 payload 标记 deleted + 删除文件。"""
        now = now or datetime.now(timezone.utc)
        removed = 0
        async with session_factory() as session:
            from sqlalchemy import select
            from app.models.context_engine import ContextPayload

            result = await session.execute(
                select(ContextPayload).where(
                    ContextPayload.expires_at.is_not(None),
                    ContextPayload.expires_at < now,
                    ContextPayload.deleted_at.is_(None),
                )
            )
            rows = list(result.scalars().all())
            for row in rows:
                row.deleted_at = now
                row.status = "deleted"
                if self._backend is not None:
                    self._backend.delete(row.user_id, row.storage_key)
                removed += 1
            await session.flush()
            await session.commit()
        return removed

    async def cleanup_orphans(
        self,
        *,
        session_factory,
    ) -> tuple[int, int]:
        """orphan cleanup：
        - DB 指向不存在文件 → 标记 deleted；
        - 文件无 DB 行 → 删除文件。
        """
        if self._backend is None:
            return 0, 0
        db_orphans = 0
        file_orphans = 0

        # 1. DB 指向不存在文件
        async with session_factory() as session:
            from sqlalchemy import select
            from app.models.context_engine import ContextPayload

            result = await session.execute(
                select(ContextPayload).where(ContextPayload.deleted_at.is_(None))
            )
            for row in list(result.scalars().all()):
                if not self._backend.exists(row.user_id, row.storage_key):
                    row.deleted_at = datetime.now(timezone.utc)
                    row.status = "missing_file"
                    db_orphans += 1
            if db_orphans:
                await session.flush()
                await session.commit()

        # 2. 文件无 DB 行
        known_keys: set[tuple[int, str]] = set()
        async with session_factory() as session:
            from sqlalchemy import select
            from app.models.context_engine import ContextPayload

            result = await session.execute(select(ContextPayload))
            for row in list(result.scalars().all()):
                known_keys.add((row.user_id, row.storage_key))

        for owner, storage_key in self._backend.iterate_orphans():
            if (owner, storage_key) not in known_keys:
                self._backend.delete(owner, storage_key)
                file_orphans += 1

        return db_orphans, file_orphans


def _gen_public_id() -> str:
    """生成 3-64 位小写字母/数字/下划线 public_id。"""
    return "pay_" + uuid.uuid4().hex[:40]


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
# auto-appended module-level note: payload storage: 文本 / 二进制 落盘(streaming)。
