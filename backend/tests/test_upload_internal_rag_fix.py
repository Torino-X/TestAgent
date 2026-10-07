"""Upload Internal RAG — targeted tests for path resolution fix.

Covers:
- TEST-UPLOAD-RAG-01: _read_file_bytes reads real file via _base prefix
- TEST-UPLOAD-RAG-02: _read_file_bytes rejects path traversal
- TEST-UPLOAD-RAG-03: _read_file_bytes rejects non-existent file
- TEST-UPLOAD-RAG-04: _read_file_bytes preserves absolute paths
- TEST-UPLOAD-RAG-05: file_service module has logger defined
- TEST-UPLOAD-RAG-06: logger.name == app.services.file_service
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.context_engine.indexing.document_service import (
    DocumentIndexError,
    IndexDocumentService,
)


# ── TEST-UPLOAD-RAG-01/02/03 ──────────────────────────────────────
class TestReadFileBytesPathResolution:
    """_read_file_bytes must resolve relative paths via local_storage._base."""

    def test_relative_path_uses_base_prefix(self):
        """Relative storage_path is joined with local_storage._base, not cwd."""
        from app.storage.local_storage import local_storage
        target_rel = "uploads/1/conv_xxx/test.txt"
        full_path = local_storage._base / target_rel
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(b"hello rag")

        try:
            raw = IndexDocumentService._read_file_bytes(target_rel)
            assert raw == b"hello rag"
        finally:
            full_path.unlink(missing_ok=True)
            full_path.parent.rmdir()

    def test_traversal_rejected(self):
        """`../../etc/passwd` is rejected with path_traversal."""
        from app.context_engine.indexing.document_service import DocumentIndexError as Exc

        with __import__("pytest").raises(Exc) as ei:
            IndexDocumentService._read_file_bytes("../outside.txt")
        assert ei.value.code == "context.index.path_traversal"

    def test_nonexistent_relative_rejected(self):
        """Relative path that resolves under _base but file missing → file_missing."""
        with __import__("pytest").raises(DocumentIndexError) as ei:
            IndexDocumentService._read_file_bytes(
                "uploads/nonexistent/does_not_exist.txt"
            )
        assert ei.value.code == "context.index.file_missing"

    def test_absolute_path_allowed(self):
        """Absolute paths are allowed (test fixtures + future direct-path cases)."""
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".txt", delete=False
        ) as f:
            f.write(b"absolute content")
            tmp_path = f.name
        try:
            raw = IndexDocumentService._read_file_bytes(tmp_path)
            assert raw == b"absolute content"
        finally:
            os.unlink(tmp_path)


# ── TEST-UPLOAD-RAG-05/06 ──────────────────────────────────────────
class TestFileServiceLogger:
    """file_service module must have a usable logger (ROOT-2 fix)."""

    def test_logger_defined(self):
        from app.services import file_service

        assert hasattr(file_service, "logger")
        assert isinstance(file_service.logger, logging.Logger)
        assert file_service.logger.name == "app.services.file_service"

    def test_logger_emits_warning_without_name_error(self, caplog):
        """Calling logger.warning must not raise NameError."""
        from app.services import file_service

        with caplog.at_level(logging.WARNING, logger="app.services.file_service"):
            file_service.logger.warning("test message | conv=%s", "conv_xxx")
        assert any("test message" in r.message for r in caplog.records)


async def test_upload_endpoint_commits_before_returning_success():
    from app.api.v1.files import upload_file

    class _Upload:
        filename = "template.docx"
        content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

        async def read(self) -> bytes:
            return b"docx bytes"

    session = SimpleNamespace(commit=AsyncMock())
    user = SimpleNamespace(internal_id=1, username="xiaoliu")
    uploaded = {
        "id": "file_committed",
        "original_name": "template.docx",
        "upload_status": "uploaded",
    }

    with patch("app.api.v1.files.FileService") as service_cls:
        service_cls.return_value.upload = AsyncMock(return_value=uploaded)

        response = await upload_file(
            file=_Upload(),
            conversation_id="conv_001",
            current_user=user,
            session=session,
        )

    service_cls.return_value.upload.assert_awaited_once()
    session.commit.assert_awaited_once()
    assert response["data"]["id"] == "file_committed"
