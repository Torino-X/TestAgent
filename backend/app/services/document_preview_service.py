"""Owner-scoped document preview preparation.

The application deliberately does not expose storage paths. This service reads
an already authorized library item and returns a bounded display model. DOCX
previews use a real Office layout engine so page structure and styling survive.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from app.core.config import get_settings
from app.core.exceptions import AgentTaskError, DocumentPreviewEngineUnavailableError, NotFoundError
from app.models.document_preview import DocumentPreview
from app.schemas.library import LibraryPreviewResponse, LibrarySpreadsheetPreview
from app.services.library_service import LibraryService
from app.storage.oss_storage import object_storage
from app.utils.datetime import utcnow
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

_MAX_TEXT_BYTES = 1_000_000
_MAX_SHEET_ROWS = 250
_MAX_SHEET_COLUMNS = 40
_WORD_EXTENSIONS = {".docx"}
_SPREADSHEET_EXTENSIONS = {".xlsx", ".csv", ".ods"}
_TEXT_EXTENSIONS = {".txt", ".log", ".json", ".xml", ".yaml", ".yml"}
_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkg": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_PREVIEW_RETRYABLE_STATES = ("queued", "failed")
_PREVIEW_PROCESSING_GRACE_SECONDS = 30
logger = logging.getLogger(__name__)


class DocumentPreviewService:
    def __init__(self, session) -> None:
        self._library = LibraryService(session)
        self._session = session
        self._preview_job_required = False

    @property
    def preview_job_required(self) -> bool:
        return self._preview_job_required

    async def get_preview(self, item_id: str, user_id: int) -> dict:
        item = await self._library.get_owned_storage_item(item_id, user_id)
        public_item = self._library._to_item(item)
        extension = self._normalized_extension(public_item.extension, public_item.name)

        if extension in _WORD_EXTENSIONS:
            preview = await self._request_word_preview(item_id, user_id, item)
            return LibraryPreviewResponse(
                item=public_item,
                viewer="word",
                pdf_url=f"/api/library/items/{item_id}/preview/pdf" if preview["status"] == "ready" else None,
                preview_status=preview["status"],
                preview_error=preview.get("error"),
            ).model_dump(mode="json")
        if extension == ".pdf":
            return LibraryPreviewResponse(
                item=public_item,
                viewer="pdf",
                pdf_url=f"/api/library/items/{item_id}/preview/pdf",
                preview_status="ready",
            ).model_dump(mode="json")
        if extension in _SPREADSHEET_EXTENSIONS:
            return LibraryPreviewResponse(
                item=public_item,
                viewer="spreadsheet",
                spreadsheet=self._parse_spreadsheet(await self._read_bytes(item.storage_path), extension),
            ).model_dump(mode="json")
        if extension == ".md":
            return LibraryPreviewResponse(
                item=public_item, viewer="markdown", text_content=self._decode_text(await self._read_bytes(item.storage_path))
            ).model_dump(mode="json")
        if extension in _TEXT_EXTENSIONS:
            return LibraryPreviewResponse(
                item=public_item, viewer="text", text_content=self._decode_text(await self._read_bytes(item.storage_path))
            ).model_dump(mode="json")
        return LibraryPreviewResponse(item=public_item, viewer="unsupported").model_dump(mode="json")

    async def get_pdf_bytes(self, item_id: str, user_id: int) -> tuple[bytes, str]:
        item = await self._library.get_owned_storage_item(item_id, user_id)
        filename = self._library._item_name(item)
        extension = self._normalized_extension(getattr(item, "file_ext", ""), filename)
        if extension in _WORD_EXTENSIONS:
            preview = await self._get_ready_preview(item_id, user_id, self._source_version(item))
            if preview is None or not preview.preview_storage_path:
                raise NotFoundError("preview is still being prepared")
            return await self._read_bytes(preview.preview_storage_path), f"{Path(filename).stem}.pdf"
        content = await self._read_bytes(item.storage_path)
        if extension == ".pdf":
            return content, filename
        if extension in _WORD_EXTENSIONS:
            try:
                preview_pdf = await asyncio.to_thread(self._docx_to_pdf, content)
            except AgentTaskError:
                raise
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                raise AgentTaskError("暂时无法生成文档预览，请稍后重试或下载文件") from exc
            return preview_pdf, f"{Path(filename).stem}.pdf"
        raise NotFoundError("可预览 PDF")

    async def get_pdf_stream(self, item_id: str, user_id: int):
        """Return a private PDF body as a stream after owner authorization."""
        item = await self._library.get_owned_storage_item(item_id, user_id)
        filename = self._library._item_name(item)
        extension = self._normalized_extension(getattr(item, "file_ext", ""), filename)
        if extension in _WORD_EXTENSIONS:
            preview = await self._get_ready_preview(item_id, user_id, self._source_version(item))
            if preview is None or not preview.preview_storage_path:
                raise NotFoundError("preview is still being prepared")
            return object_storage.open_file(preview.preview_storage_path), f"{Path(filename).stem}.pdf"
        if extension == ".pdf":
            return object_storage.open_file(item.storage_path), filename
        raise NotFoundError("可预览 PDF")

    async def _request_word_preview(self, item_id: str, user_id: int, item) -> dict:
        """Return durable state and queue a missing DOCX derivative.

        The database row is the authoritative cross-worker claim. Redis holds
        only this tiny state payload so polling is cheap; PDF bytes never enter
        Redis.
        """
        source_version = self._source_version(item)
        from app.cache.domains.document_preview_cache import get_document_preview_cache

        cache = get_document_preview_cache()
        cached = await cache.get(user_id, item_id)
        if cached and cached.get("source_version") == source_version:
            if cached.get("status") == "processing" and not self._cached_processing_is_stale(cached):
                return cached

        # A queued cache entry is only a polling hint, never a durable job
        # claim.  In particular, Redis can outlive a DB reset or a cancelled
        # request, leaving a queued entry without a document_previews row.
        # Fall through to the DB so the task can be recreated and scheduled.
        preview = await self._find_preview(item_id, user_id)
        if preview is None:
            preview = DocumentPreview(item_public_id=item_id, user_id=user_id, source_version=source_version, status="queued")
            self._session.add(preview)
            await self._session.flush()
            self._preview_job_required = True
        elif preview.source_version != source_version or preview.status == "failed":
            preview.source_version = source_version
            preview.status = "queued"
            preview.error_message = None
            preview.updated_at = utcnow()
            self._preview_job_required = True
        elif preview.status == "ready":
            # Object storage is the source of truth for the binary derivative.
            # A ready metadata row without its private object must regenerate.
            if not preview.preview_storage_path or not await object_storage.exists(preview.preview_storage_path):
                preview.status = "queued"
                preview.preview_storage_path = None
                preview.preview_size = None
                preview.error_message = None
                preview.updated_at = utcnow()
                self._preview_job_required = True
        elif preview.status == "processing" and self._processing_is_stale(preview):
            # A worker can be killed after it claimed the job. Requeue only
            # after the Office timeout plus a grace window has elapsed.
            preview.status = "queued"
            preview.error_message = None
            preview.updated_at = utcnow()
            self._preview_job_required = True
        elif preview.status == "queued":
            # Safe recovery after a worker/process restart: the job's DB claim
            # prevents two request paths from converting the same DOCX.
            self._preview_job_required = True

        result = self._status_payload(preview)
        await cache.set(user_id, item_id, result)
        return result

    async def _get_ready_preview(self, item_id: str, user_id: int, source_version: str) -> DocumentPreview | None:
        preview = await self._find_preview(item_id, user_id)
        if preview is None or preview.status != "ready" or preview.source_version != source_version:
            return None
        if not preview.preview_storage_path or not await object_storage.exists(preview.preview_storage_path):
            preview.status = "queued"
            preview.preview_storage_path = None
            preview.preview_size = None
            preview.updated_at = utcnow()
            await self._session.flush()
            return None
        return preview

    async def _find_preview(self, item_id: str, user_id: int) -> DocumentPreview | None:
        result = await self._session.execute(
            select(DocumentPreview).where(
                DocumentPreview.item_public_id == item_id,
                DocumentPreview.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def purge_preview(self, item_id: str, user_id: int) -> None:
        """Remove a derivative when its source is permanently removed."""
        preview = await self._find_preview(item_id, user_id)
        if preview is None:
            return
        storage_path = preview.preview_storage_path
        await self._session.delete(preview)
        await self._session.flush()
        from app.cache.domains.document_preview_cache import get_document_preview_cache

        await get_document_preview_cache().delete(user_id, item_id)
        if storage_path:
            await object_storage.delete_file(storage_path)

    @staticmethod
    def _source_version(item) -> str:
        file_hash = (getattr(item, "file_hash", None) or "").strip()
        if file_hash:
            return f"hash:{file_hash}"
        return f"version:{getattr(item, 'version_no', None) or 1}:storage:{getattr(item, 'storage_path', '')}"

    @staticmethod
    def _status_payload(preview: DocumentPreview) -> dict:
        updated_at = getattr(preview, "updated_at", None)
        return {
            "status": preview.status,
            "source_version": preview.source_version,
            "error": preview.error_message,
            "updated_at": updated_at.isoformat() if updated_at else None,
        }

    @staticmethod
    def _cached_processing_is_stale(cached: dict) -> bool:
        value = cached.get("updated_at")
        if not value:
            return False
        try:
            updated_at = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return updated_at <= DocumentPreviewService._stale_before()

    @staticmethod
    def _processing_is_stale(preview: DocumentPreview) -> bool:
        updated_at = getattr(preview, "updated_at", None)
        if updated_at is None:
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return updated_at <= DocumentPreviewService._stale_before()

    @staticmethod
    def _stale_before():
        timeout = get_settings().document_preview_office_timeout_seconds
        return utcnow() - timedelta(seconds=timeout + _PREVIEW_PROCESSING_GRACE_SECONDS)

    @classmethod
    async def build_preview_job(cls, item_id: str, user_id: int) -> None:
        """Background job with a fresh session; HTTP latency is never tied to Office."""
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            # Redis is an optimization-only cross-worker admission lock. The
            # conditional DB update below remains the durable correctness guard
            # when Redis is disabled or temporarily unavailable.
            from app.cache.backend import CacheBackend
            from app.cache.distributed_lock import CacheFillLock

            try:
                backend = CacheBackend.get()
            except RuntimeError:
                backend = None
            if backend is not None and backend.is_enabled:
                lock = CacheFillLock(
                    backend.client,
                    ttl_ms=(get_settings().document_preview_office_timeout_seconds + 30) * 1000,
                    key_prefix="ta:preview-lock:",
                )
                owner = await lock.acquire(f"user:{user_id}:item:{item_id}")
                if owner is None:
                    return
                async with owner:
                    await cls(session)._build_queued_preview(item_id, user_id)
            else:
                await cls(session)._build_queued_preview(item_id, user_id)

    async def _build_queued_preview(self, item_id: str, user_id: int) -> None:
        cache = None
        try:
            claim = await self._session.execute(
                update(DocumentPreview)
                .where(
                    DocumentPreview.item_public_id == item_id,
                    DocumentPreview.user_id == user_id,
                    DocumentPreview.status.in_(_PREVIEW_RETRYABLE_STATES),
                )
                .values(status="processing", error_message=None, updated_at=utcnow())
            )
            if not claim.rowcount:
                return
            await self._session.commit()
            from app.cache.domains.document_preview_cache import get_document_preview_cache

            cache = get_document_preview_cache()
            item = await self._library.get_owned_storage_item(item_id, user_id)
            filename = self._library._item_name(item)
            source_version = self._source_version(item)
            preview = await self._find_preview(item_id, user_id)
            if preview is None or preview.source_version != source_version:
                return
            await cache.set(user_id, item_id, self._status_payload(preview))
            content = await self._read_bytes(item.storage_path)
            pdf = await asyncio.to_thread(self._docx_to_pdf, content)
            stored = await object_storage.save_preview(pdf, f"{Path(filename).stem}.pdf", str(user_id), item_id)
            old_path = preview.preview_storage_path
            preview.status = "ready"
            preview.preview_storage_path = stored.storage_path
            preview.preview_size = stored.file_size
            preview.error_message = None
            preview.updated_at = utcnow()
            await self._session.commit()
            await cache.set(user_id, item_id, self._status_payload(preview))
            if old_path and old_path != stored.storage_path:
                await object_storage.delete_file(old_path)
            logger.info("Document preview ready | item=%s user=%s bytes=%s", item_id, user_id, stored.file_size)
        except OperationalError as exc:
            await self._session.rollback()
            if self._is_retryable_db_contention(exc):
                logger.warning(
                    "Document preview claim deferred by database contention | item=%s user=%s",
                    item_id,
                    user_id,
                )
                return
            await self._record_preview_failure(item_id, user_id, cache)
            logger.exception("Document preview failed | item=%s user=%s", item_id, user_id, exc_info=exc)
        except Exception as exc:  # noqa: BLE001 - durable failure becomes a frontend-visible state
            await self._session.rollback()
            await self._record_preview_failure(item_id, user_id, cache)
            logger.exception("Document preview failed | item=%s user=%s", item_id, user_id, exc_info=exc)

    @staticmethod
    def _is_retryable_db_contention(exc: OperationalError) -> bool:
        code = next(iter(getattr(getattr(exc, "orig", None), "args", ())), None)
        return code in {1205, 1213}

    async def _record_preview_failure(self, item_id: str, user_id: int, cache) -> None:
        preview = await self._find_preview(item_id, user_id)
        if preview is None:
            return
        preview.status = "failed"
        preview.error_message = "Preview preparation failed. Please retry."
        preview.updated_at = utcnow()
        await self._session.commit()
        if cache is None:
            from app.cache.domains.document_preview_cache import get_document_preview_cache

            cache = get_document_preview_cache()
        await cache.set(user_id, item_id, self._status_payload(preview))

    @staticmethod
    def _normalized_extension(extension: str | None, filename: str) -> str:
        value = (extension or Path(filename).suffix).strip().lower()
        return f".{value}" if value and not value.startswith(".") else value

    async def _read_bytes(self, storage_path: str) -> bytes:
        if not storage_path:
            raise NotFoundError("文件")
        chunks = []
        total = 0
        async for chunk in object_storage.open_file(storage_path):
            total += len(chunk)
            if total > _MAX_TEXT_BYTES * 16:
                raise NotFoundError("文件")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _decode_text(content: bytes) -> str:
        sample = content[:_MAX_TEXT_BYTES]
        for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
            try:
                return sample.decode(encoding)
            except UnicodeDecodeError:
                continue
        return sample.decode("utf-8", errors="replace")

    def _parse_spreadsheet(self, content: bytes, extension: str) -> LibrarySpreadsheetPreview:
        if extension == ".csv":
            import csv

            rows = list(csv.reader(io.StringIO(self._decode_text(content))))[:_MAX_SHEET_ROWS]
            return self._worksheet_model("Sheet1", rows)
        if extension != ".xlsx":
            return LibrarySpreadsheetPreview(sheet_name="Sheet1", columns=[], rows=[], truncated=False)
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                shared_strings = self._shared_strings(archive)
                sheet_name, sheet_path = self._first_sheet(archive)
                root = ET.fromstring(archive.read(sheet_path))
                rows: list[list[str]] = []
                max_column = 0
                for row in root.findall(".//main:sheetData/main:row", _NS):
                    values: list[str] = []
                    for cell in row.findall("main:c", _NS):
                        column = self._column_index(cell.get("r", "A1"))
                        if column >= _MAX_SHEET_COLUMNS:
                            continue
                        if len(values) <= column:
                            values.extend([""] * (column + 1 - len(values)))
                        values[column] = self._cell_value(cell, shared_strings)
                        max_column = max(max_column, column + 1)
                    rows.append(values)
                    if len(rows) >= _MAX_SHEET_ROWS:
                        break
                normalized_rows = [row[:max_column] for row in rows]
                return self._worksheet_model(sheet_name, normalized_rows, truncated=len(root.findall(".//main:sheetData/main:row", _NS)) > len(rows))
        except (KeyError, ET.ParseError, zipfile.BadZipFile):
            return LibrarySpreadsheetPreview(sheet_name="Sheet1", columns=[], rows=[], truncated=False)

    @staticmethod
    def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
        try:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        except KeyError:
            return []
        return ["".join(node.itertext()) for node in root.findall("main:si", _NS)]

    @staticmethod
    def _first_sheet(archive: zipfile.ZipFile) -> tuple[str, str]:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheet = workbook.find("main:sheets/main:sheet", _NS)
        if sheet is None:
            raise KeyError("worksheet")
        rel_id = sheet.get(f"{{{_NS['rel']}}}id")
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        target = next(node.get("Target") for node in rels.findall("pkg:Relationship", _NS) if node.get("Id") == rel_id)
        return sheet.get("name") or "Sheet1", f"xl/{target.lstrip('/')}"

    @staticmethod
    def _cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
        value = cell.findtext("main:v", default="", namespaces=_NS)
        cell_type = cell.get("t")
        if cell_type == "s" and value.isdigit() and int(value) < len(shared_strings):
            return shared_strings[int(value)]
        if cell_type == "inlineStr":
            return "".join(cell.find("main:is", _NS).itertext()) if cell.find("main:is", _NS) is not None else ""
        formula = cell.findtext("main:f", default="", namespaces=_NS)
        return f"={formula}" if formula and not value else value

    @staticmethod
    def _column_index(reference: str) -> int:
        letters = re.match(r"[A-Z]+", reference.upper())
        result = 0
        for letter in (letters.group(0) if letters else "A"):
            result = result * 26 + ord(letter) - 64
        return result - 1

    @staticmethod
    def _worksheet_model(sheet_name: str, rows: list[list[str]], truncated: bool = False) -> LibrarySpreadsheetPreview:
        width = min(_MAX_SHEET_COLUMNS, max((len(row) for row in rows), default=1))
        columns = [DocumentPreviewService._column_name(index) for index in range(width)]
        return LibrarySpreadsheetPreview(sheet_name=sheet_name, columns=columns, rows=[row[:width] + [""] * max(0, width - len(row)) for row in rows], truncated=truncated)

    @staticmethod
    def _column_name(index: int) -> str:
        result = ""
        index += 1
        while index:
            index, remainder = divmod(index - 1, 26)
            result = chr(65 + remainder) + result
        return result

    def _docx_to_pdf(self, content: bytes) -> bytes:
        """Render original OOXML with Office instead of rebuilding document text.

        The source and generated PDF live only in a request-scoped temporary
        directory. The context manager removes them on every success or error;
        durable file data remains exclusively in OSS.
        """
        settings = get_settings()
        office_binary = self._office_binary()
        if Path(office_binary).name.casefold() == "winword.exe":
            try:
                return self._docx_to_pdf_with_word(content)
            except RuntimeError as exc:
                raise DocumentPreviewEngineUnavailableError() from exc

        with tempfile.TemporaryDirectory(prefix="testagent-doc-preview-") as workspace:
            workspace_path = Path(workspace)
            source_path = workspace_path / "document.docx"
            output_path = workspace_path / "output"
            profile_path = workspace_path / "office-profile"
            output_path.mkdir()
            profile_path.mkdir()
            source_path.write_bytes(content)

            result = subprocess.run(
                [
                    office_binary,
                    "--headless",
                    "--nologo",
                    "--nodefault",
                    "--nolockcheck",
                    "--nofirststartwizard",
                    f"-env:UserInstallation={profile_path.as_uri()}",
                    "--convert-to",
                    "pdf:writer_pdf_Export",
                    "--outdir",
                    str(output_path),
                    str(source_path),
                ],
                check=False,
                capture_output=True,
                timeout=settings.document_preview_office_timeout_seconds,
            )
            pdf_path = output_path / "document.pdf"
            if result.returncode != 0 or not pdf_path.is_file() or pdf_path.stat().st_size == 0:
                raise RuntimeError("Office document conversion did not produce a PDF")
            return pdf_path.read_bytes()

    @staticmethod
    def _docx_to_pdf_with_word(content: bytes) -> bytes:
        """Use Word's native exporter when the API process has desktop access."""
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise RuntimeError("Windows Word automation dependency is unavailable") from exc

        word = None
        document = None
        initialized = False
        try:
            pythoncom.CoInitialize()
            initialized = True
            with tempfile.TemporaryDirectory(prefix="testagent-word-preview-") as workspace:
                workspace_path = Path(workspace)
                source_path = workspace_path / "document.docx"
                pdf_path = workspace_path / "document.pdf"
                source_path.write_bytes(content)

                word = win32com.client.DispatchEx("Word.Application")
                word.Visible = False
                word.DisplayAlerts = 0
                # msoAutomationSecurityForceDisable: opening an uploaded file
                # for preview must never run a document macro.
                word.AutomationSecurity = 3
                document = word.Documents.Open(str(source_path), ReadOnly=True, AddToRecentFiles=False)
                document.ExportAsFixedFormat(str(pdf_path), 17, OpenAfterExport=False)
                if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
                    raise RuntimeError("Word did not produce a PDF")
                return pdf_path.read_bytes()
        except Exception as exc:
            raise RuntimeError("Native Word PDF conversion is unavailable") from exc
        finally:
            if document is not None:
                try:
                    document.Close(SaveChanges=0)
                except Exception:
                    pass
            if word is not None:
                try:
                    word.Quit(SaveChanges=0)
                except Exception:
                    pass
            if initialized:
                pythoncom.CoUninitialize()

    @staticmethod
    def _office_binary() -> str:
        configured_path = get_settings().document_preview_office_bin.strip()
        if configured_path:
            if Path(configured_path).is_file():
                return configured_path
            raise DocumentPreviewEngineUnavailableError()

        discovered = [
            str(Path(r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE")),
            str(Path(r"C:\Program Files (x86)\Microsoft Office\root\Office16\WINWORD.EXE")),
            shutil.which("soffice.com"),
            shutil.which("soffice"),
            str(Path(r"C:\Program Files\LibreOffice\program\soffice.com")),
            "/usr/bin/libreoffice",
            "/usr/bin/soffice",
        ]
        office_binary = next((path for path in discovered if path and Path(path).is_file()), None)
        if office_binary is None:
            raise DocumentPreviewEngineUnavailableError()
        return office_binary
