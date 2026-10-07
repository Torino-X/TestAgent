"""Narrow, owner-scoped query for the Library metadata list."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact import Artifact
from app.models.uploaded_file import UploadedFile


LibraryCategory = Literal["all", "image", "file"]
LibraryScope = Literal["active", "deleted"]
LibrarySourceFilter = Literal["all", "upload", "generated"]
LibraryFileType = Literal["all", "image", "document", "spreadsheet", "presentation", "pdf"]

_IMAGE_EXTENSIONS = (".avif", ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp")
_SPREADSHEET_EXTENSIONS = (".csv", ".ods", ".xls", ".xlsx")
_PRESENTATION_EXTENSIONS = (".odp", ".ppt", ".pptx")


@dataclass(frozen=True)
class LibraryMetadataRow:
    public_id: str
    name: str
    source: Literal["upload", "generated"]
    mime_type: str | None
    extension: str
    size_bytes: int | None
    modified_at: object
    artifact_type: str | None
    deleted_at: object | None


@dataclass(frozen=True)
class LibraryMetadataPage:
    rows: list[LibraryMetadataRow]
    total: int


class LibraryRepository:
    """Read only list metadata; it never selects storage paths or artifact JSON."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_metadata(
        self,
        user_id: int,
        *,
        category: LibraryCategory,
        query: str,
        scope: LibraryScope,
        source: LibrarySourceFilter,
        file_type: LibraryFileType,
        page: int,
        page_size: int,
    ) -> LibraryMetadataPage:
        listing = self._source_union(user_id, scope=scope, source=source)
        extension = func.lower(func.coalesce(listing.c.extension, ""))
        mime_type = func.lower(func.coalesce(listing.c.mime_type, ""))
        image = or_(mime_type.like("image/%"), extension.in_(_IMAGE_EXTENSIONS))
        predicates = []

        if query.strip():
            predicates.append(func.lower(listing.c.name).like(f"%{query.strip().lower()}%"))
        if category == "image":
            predicates.append(image)
        elif category == "file":
            predicates.append(~image)
        if file_type == "image":
            predicates.append(image)
        elif file_type == "pdf":
            predicates.append(extension == ".pdf")
        elif file_type == "spreadsheet":
            predicates.append(extension.in_(_SPREADSHEET_EXTENSIONS))
        elif file_type == "presentation":
            predicates.append(extension.in_(_PRESENTATION_EXTENSIONS))
        elif file_type == "document":
            predicates.extend((
                ~image,
                extension != ".pdf",
                ~extension.in_(_SPREADSHEET_EXTENSIONS),
                ~extension.in_(_PRESENTATION_EXTENSIONS),
            ))

        order_column = listing.c.deleted_at if scope == "deleted" else listing.c.modified_at
        statement = (
            select(listing, func.count().over().label("_total"))
            .where(*predicates)
            .order_by(order_column.desc(), listing.c.public_id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = (await self.session.execute(statement)).mappings().all()
        if not result:
            return LibraryMetadataPage(rows=[], total=0)

        rows = [
            LibraryMetadataRow(
                public_id=str(row["public_id"]),
                name=str(row["name"]),
                source=row["source"],
                mime_type=row["mime_type"],
                extension=row["extension"] or "",
                size_bytes=row["size_bytes"],
                modified_at=row["modified_at"],
                artifact_type=row["artifact_type"],
                deleted_at=row["deleted_at"],
            )
            for row in result
        ]
        return LibraryMetadataPage(rows=rows, total=int(result[0]["_total"]))

    @staticmethod
    def _source_union(user_id: int, *, scope: LibraryScope, source: LibrarySourceFilter):
        queries = []
        if source in {"all", "upload"}:
            upload_query = select(
                UploadedFile.public_id.label("public_id"),
                UploadedFile.original_name.label("name"),
                literal("upload").label("source"),
                UploadedFile.mime_type.label("mime_type"),
                UploadedFile.file_ext.label("extension"),
                UploadedFile.file_size.label("size_bytes"),
                UploadedFile.updated_at.label("modified_at"),
                literal(None).label("artifact_type"),
                UploadedFile.deleted_at.label("deleted_at"),
            ).where(UploadedFile.user_id == user_id)
            upload_query = upload_query.where(
                UploadedFile.deleted_at.is_not(None) if scope == "deleted" else UploadedFile.deleted_at.is_(None)
            )
            queries.append(upload_query)
        if source in {"all", "generated"}:
            artifact_query = select(
                Artifact.public_id.label("public_id"),
                Artifact.file_name.label("name"),
                literal("generated").label("source"),
                Artifact.mime_type.label("mime_type"),
                Artifact.file_ext.label("extension"),
                Artifact.file_size.label("size_bytes"),
                Artifact.updated_at.label("modified_at"),
                Artifact.artifact_type.label("artifact_type"),
                Artifact.deleted_at.label("deleted_at"),
            ).where(Artifact.user_id == user_id)
            if scope == "deleted":
                artifact_query = artifact_query.where(Artifact.deleted_at.is_not(None))
            else:
                artifact_query = artifact_query.where(Artifact.status == "available", Artifact.deleted_at.is_(None))
            queries.append(artifact_query)
        return union_all(*queries).subquery("library_metadata")
