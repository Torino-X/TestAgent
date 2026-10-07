"""Public contracts for the user library. File contents stay private to storage."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


LibraryCategory = Literal["all", "image", "file"]
LibraryScope = Literal["active", "deleted"]
LibrarySourceFilter = Literal["all", "upload", "generated"]
LibraryFileType = Literal["all", "image", "document", "spreadsheet", "presentation", "pdf"]
LibraryItemKind = Literal["image", "file"]
LibraryItemSource = Literal["upload", "generated", "template"]


class LibraryItem(BaseModel):
    """A normalized, owner-scoped uploaded file or generated artifact."""

    id: str
    name: str
    kind: LibraryItemKind
    source: LibraryItemSource
    mime_type: str | None = None
    extension: str = ""
    size_bytes: int | None = None
    modified_at: datetime
    conversation_id: str | None = None
    artifact_type: str | None = None
    thumbnail_url: str | None = None
    download_url: str | None = None
    deleted_at: datetime | None = None


class LibraryItemListResponse(BaseModel):
    items: list[LibraryItem] = Field(default_factory=list)
    total: int = 0


class LibraryRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class LibrarySpreadsheetPreview(BaseModel):
    """A bounded, display-ready worksheet snapshot."""

    sheet_name: str = "Sheet1"
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    truncated: bool = False


class LibraryPreviewResponse(BaseModel):
    """Safe preview metadata. Binary bytes are served by a separate endpoint."""

    item: LibraryItem
    viewer: Literal["word", "spreadsheet", "pdf", "markdown", "text", "unsupported"]
    text_content: str | None = None
    spreadsheet: LibrarySpreadsheetPreview | None = None
    pdf_url: str | None = None
    preview_status: Literal["queued", "processing", "ready", "failed"] | None = None
    preview_error: str | None = None
