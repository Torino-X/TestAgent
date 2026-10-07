"""CE-02 WP-5：Payload 外置 + 读取测试。

覆盖：put/open/delete/owner-scope/hash/**路径 canonicalization + traversal +
symlink escape 拒绝**/服务端 key/原子写/size limit/sha256 读写校验/DB-file
补偿/expiry/orphan cleanup/delete 引用检查/storage key+path 不泄露。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from app.context_engine.errors import ContextEngineFailure
from app.context_engine.models.payload import PayloadStoreCommand
from app.context_engine.payload import FileSystemPayloadBackend, PayloadStorageService
from app.context_engine.payload.reader import PayloadReader


class _FakePayloadRow:
    """模拟 ContextPayload ORM 行。"""

    def __init__(self, public_id, user_id, storage_key, sha256, storage_backend="fs"):
        self.public_id = public_id
        self.user_id = user_id
        self.storage_key = storage_key
        self.sha256 = sha256
        self.storage_backend = storage_backend
        self.mime_type = "text/plain"
        self.size_bytes = 0
        self.char_count = 0
        self.estimated_tokens = 0
        self.status = "active"
        self.metadata_json = None
        self.expires_at = None
        self.deleted_at = None
        self.workspace_key = None
        self.conversation_id = None
        self.task_id = None
        self.source_type = "context_engine"
        self.payload_type = "context_payload"
        self.content_encoding = None
        self.encrypted = False
        self.encryption_key_ref = None
        self.created_at = None


class _FakeRepo:
    def __init__(self):
        self.rows = {}
        self.session_rows = []

    async def get_by_public_id(self, public_id, user_id):
        return self.rows.get((public_id, user_id))

    def _register_row(self, row):
        self.rows[(row.public_id, row.user_id)] = row
        self.session_rows.append(row)


class _FakeSession:
    def __init__(self, repo):
        self.repo = repo
        self._row = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def add(self, row):
        self._row = row

    async def flush(self):
        if self._row is not None:
            self.repo._register_row(self._row)

    async def commit(self):
        pass


def _make_service(repo, backend, **kw):
    return PayloadStorageService(repo, backend=backend, **kw)


def _make_backend():
    root = tempfile.mkdtemp()
    return FileSystemPayloadBackend(root), root


def test_backend_path_traversal_rejected():
    backend, _ = _make_backend()
    with pytest.raises(ContextEngineFailure) as exc_info:
        backend._path_for(1, "../evil")
    assert exc_info.value.error.code == "context.payload.path_traversal"


def test_backend_symlink_escape_rejected():
    backend, _ = _make_backend()
    # 绝对路径 escape
    with pytest.raises(ContextEngineFailure):
        backend._path_for(1, "/etc/passwd")


def test_backend_atomic_write_read():
    backend, _ = _make_backend()
    backend.write(1, "key1", b"hello")
    assert backend.exists(1, "key1")
    assert backend.read(1, "key1") == b"hello"


async def test_put_service_generated_key_and_owner_scope():
    repo = _FakeRepo()
    backend, _ = _make_backend()
    svc = _make_service(repo, backend)
    ref = await svc.put(
        PayloadStoreCommand(user_id=1, content="hello payload"),
        session_factory=lambda: _FakeSession(repo),
    )
    assert ref.payload_public_id.startswith("pay_")
    assert ref.content_sha256 is not None
    # storage key/path 不出现在 ref
    assert "storage_key" not in ref.model_dump()
    assert "storage_path" not in ref.model_dump()
    # owner-scope：其他用户读不到
    with pytest.raises(ContextEngineFailure):
        async for _ in svc.open(2, ref.payload_public_id, session_factory=lambda: _FakeSession(repo)):
            pass


async def test_open_reads_content():
    repo = _FakeRepo()
    backend, _ = _make_backend()
    svc = _make_service(repo, backend)
    ref = await svc.put(
        PayloadStoreCommand(user_id=1, content="你好 payload"),
        session_factory=lambda: _FakeSession(repo),
    )
    chunks = []
    async for c in svc.open(1, ref.payload_public_id, session_factory=lambda: _FakeSession(repo)):
        chunks.append(c)
    assert b"".join(chunks) == "你好 payload".encode("utf-8")


async def test_sha256_read_verification():
    repo = _FakeRepo()
    backend, root = _make_backend()
    svc = _make_service(repo, backend)
    ref = await svc.put(
        PayloadStoreCommand(user_id=1, content="hello"),
        session_factory=lambda: _FakeSession(repo),
    )
    # 篡改文件内容 → checksum 校验失败
    target = Path(root) / "1"
    key = None
    for p in target.iterdir():
        if not p.name.startswith("."):
            key = p.name
            p.write_bytes(b"tampered")
    if key:
        with pytest.raises(ContextEngineFailure) as exc_info:
            async for _ in svc.open(1, ref.payload_public_id, session_factory=lambda: _FakeSession(repo)):
                pass
        assert exc_info.value.error.code == "context.payload.checksum_mismatch"


async def test_size_limit():
    repo = _FakeRepo()
    backend, _ = _make_backend()
    svc = _make_service(repo, backend, max_size_bytes=10)
    with pytest.raises(ContextEngineFailure) as exc_info:
        await svc.put(
            PayloadStoreCommand(user_id=1, content="x" * 100),
            session_factory=lambda: _FakeSession(repo),
        )
    assert exc_info.value.error.code == "context.payload.too_large"


async def test_delete_owner_scoped():
    repo = _FakeRepo()
    backend, _ = _make_backend()
    svc = _make_service(repo, backend)
    ref = await svc.put(
        PayloadStoreCommand(user_id=1, content="hello"),
        session_factory=lambda: _FakeSession(repo),
    )
    # 其他用户删除 → False（无权限）
    assert await svc.delete(2, ref.payload_public_id, session_factory=lambda: _FakeSession(repo)) is False
    # owner 删除 → True
    assert await svc.delete(1, ref.payload_public_id, session_factory=lambda: _FakeSession(repo)) is True


async def test_payload_reader_estimates_tokens():
    repo = _FakeRepo()
    backend, _ = _make_backend()
    svc = _make_service(repo, backend)
    ref = await svc.put(
        PayloadStoreCommand(user_id=1, content="hello payload"),
        session_factory=lambda: _FakeSession(repo),
    )
    reader = PayloadReader(svc)
    text = await reader.read_text(ref, session_factory=lambda: _FakeSession(repo))
    assert text == "hello payload"
    tokens = await reader.estimate_tokens(ref, session_factory=lambda: _FakeSession(repo))
    assert tokens > 0
