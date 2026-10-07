from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.library_service import LibraryService
from app.repositories.library_repository import LibraryMetadataPage, LibraryMetadataRow


class InMemoryLibraryRepository:
    def __init__(self, uploads, artifacts) -> None:
        self._rows = [
            LibraryMetadataRow(
                public_id=upload.public_id,
                name=upload.original_name,
                source="upload",
                mime_type=upload.mime_type,
                extension=upload.file_ext,
                size_bytes=upload.file_size,
                modified_at=upload.updated_at or upload.created_at,
                artifact_type=None,
                deleted_at=upload.deleted_at,
            )
            for upload in uploads
        ] + [
            LibraryMetadataRow(
                public_id=artifact.public_id,
                name=artifact.file_name,
                source="generated",
                mime_type=artifact.mime_type,
                extension=artifact.file_ext,
                size_bytes=artifact.file_size,
                modified_at=artifact.updated_at or artifact.created_at,
                artifact_type=artifact.artifact_type,
                deleted_at=artifact.deleted_at,
            )
            for artifact in artifacts
        ]

    async def list_metadata(self, _user_id, *, category, query, scope, source, file_type, page, page_size):
        def is_image(row):
            return (row.mime_type or "").startswith("image/") or row.extension.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

        rows = [row for row in self._rows if (row.deleted_at is not None) == (scope == "deleted")]
        rows = [row for row in rows if source == "all" or row.source == source]
        rows = [row for row in rows if category == "all" or ("image" if is_image(row) else "file") == category]
        if file_type == "image":
            rows = [row for row in rows if is_image(row)]
        elif file_type == "spreadsheet":
            rows = [row for row in rows if row.extension.lower() in {".xls", ".xlsx", ".csv", ".ods"}]
        elif file_type == "pdf":
            rows = [row for row in rows if row.extension.lower() == ".pdf"]
        elif file_type == "presentation":
            rows = [row for row in rows if row.extension.lower() in {".ppt", ".pptx", ".odp"}]
        elif file_type == "document":
            rows = [row for row in rows if not is_image(row) and row.extension.lower() not in {".pdf", ".xls", ".xlsx", ".csv", ".ods", ".ppt", ".pptx", ".odp"}]
        if query.strip():
            rows = [row for row in rows if query.strip().casefold() in row.name.casefold()]
        rows.sort(key=lambda row: row.deleted_at or row.modified_at, reverse=True)
        total = len(rows)
        start = (page - 1) * page_size
        return LibraryMetadataPage(rows=rows[start:start + page_size], total=total)


def make_service(*, uploads, artifacts) -> LibraryService:
    service = LibraryService.__new__(LibraryService)
    service._session = MagicMock(commit=AsyncMock())
    service._file_repo = MagicMock(
        list_by_user=AsyncMock(return_value=uploads), list_deleted_by_user=AsyncMock(return_value=[]),
        rename=AsyncMock(), soft_delete=AsyncMock(), restore=AsyncMock(), hard_delete=AsyncMock(),
    )
    service._artifact_repo = MagicMock(
        list_by_user=AsyncMock(return_value=artifacts), list_deleted_by_user=AsyncMock(return_value=[]),
        rename=AsyncMock(), soft_delete=AsyncMock(), restore=AsyncMock(), hard_delete=AsyncMock(),
    )
    service._library_repo = InMemoryLibraryRepository(uploads, artifacts)
    return service


def upload_row(**overrides):
    values = {
        "public_id": "file_requirements",
        "user_id": 7,
        "original_name": "需求说明.md",
        "mime_type": "text/markdown",
        "file_ext": ".md",
        "file_size": 12000,
        "created_at": datetime(2026, 9, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 3, tzinfo=UTC),
        "storage_path": "private/never-exposed",
        "deleted_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def artifact_row(**overrides):
    values = {
        "public_id": "art_test_plan",
        "user_id": 7,
        "status": "available",
        "file_name": "测试方案.docx",
        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "file_ext": ".docx",
        "file_size": 28000,
        "artifact_type": "test_plan_word",
        "created_at": datetime(2026, 9, 2, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 4, tzinfo=UTC),
        "storage_path": "private/generated/never-exposed",
        "deleted_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_library_merges_uploads_and_artifacts_without_private_storage_metadata():
    service = make_service(uploads=[upload_row()], artifacts=[artifact_row()])

    items, total = await service.list_items(7)

    assert total == 2
    assert [item["id"] for item in items] == ["art_test_plan", "file_requirements"]
    assert items[0]["source"] == "generated"
    assert items[0]["download_url"] == "/api/library/items/art_test_plan/download"
    assert all("storage_path" not in item for item in items)


@pytest.mark.asyncio
async def test_library_filters_images_and_searches_across_sources():
    image = upload_row(
        public_id="file_flow_image",
        original_name="流程图.png",
        mime_type="image/png",
        file_ext=".png",
        updated_at=datetime.now(UTC) + timedelta(days=1),
    )
    service = make_service(uploads=[upload_row(), image], artifacts=[artifact_row()])

    images, image_total = await service.list_items(7, category="image")
    searched, searched_total = await service.list_items(7, query="测试方案")

    assert image_total == 1
    assert images[0]["id"] == "file_flow_image"
    assert searched_total == 1
    assert searched[0]["id"] == "art_test_plan"


@pytest.mark.asyncio
async def test_library_filters_source_and_deleted_scope():
    deleted = upload_row(public_id="file_deleted", deleted_at=datetime.now(UTC), updated_at=datetime.now(UTC))
    service = make_service(uploads=[upload_row(), deleted], artifacts=[artifact_row()])

    generated, total = await service.list_items(7, source="generated")
    recycle_bin, deleted_total = await service.list_items(7, scope="deleted")

    assert total == 1
    assert generated[0]["id"] == "art_test_plan"
    assert deleted_total == 1
    assert recycle_bin[0]["id"] == "file_deleted"
    assert recycle_bin[0]["deleted_at"] is not None


@pytest.mark.asyncio
async def test_library_pages_the_sorted_metadata_result():
    newest = upload_row(public_id="file_newest", updated_at=datetime(2026, 9, 5, tzinfo=UTC))
    middle = upload_row(public_id="file_middle", updated_at=datetime(2026, 9, 4, tzinfo=UTC))
    oldest = upload_row(public_id="file_oldest", updated_at=datetime(2026, 9, 3, tzinfo=UTC))
    service = make_service(uploads=[newest, middle, oldest], artifacts=[])

    items, total = await service.list_items(7, page=2, page_size=1)

    assert total == 3
    assert [item["id"] for item in items] == ["file_middle"]


@pytest.mark.asyncio
async def test_library_list_uses_the_per_user_metadata_cache(monkeypatch):
    service = make_service(uploads=[upload_row()], artifacts=[artifact_row()])

    class CacheSpy:
        calls: list[tuple[int, str]] = []

        async def get_or_load_list(self, user_id, loader, *, filter_hash):
            self.calls.append((user_id, filter_hash))
            return await loader()

    cache = CacheSpy()
    from app.cache.domains import library_cache

    monkeypatch.setattr(library_cache, "get_library_cache", lambda: cache)

    items, total = await service.list_items(7, category="file")

    assert total == 2
    assert items[0]["id"] == "art_test_plan"
    assert cache.calls and cache.calls[0][0] == 7
    assert cache.calls[0][1] != "all"


@pytest.mark.asyncio
async def test_library_soft_delete_rename_and_restore_are_owner_scoped():
    upload = upload_row()
    service = make_service(uploads=[], artifacts=[])
    service._file_repo.get_any_by_public_id = AsyncMock(return_value=upload)

    renamed = await service.rename("file_requirements", "renamed.md", 7)
    await service.soft_delete("file_requirements", 7)
    upload.deleted_at = datetime.now(UTC)
    await service.restore("file_requirements", 7)

    assert renamed["name"] == "renamed.md"
    service._file_repo.rename.assert_awaited_once()
    service._file_repo.soft_delete.assert_awaited_once()
    service._file_repo.restore.assert_awaited_once()
    assert service._session.commit.await_count == 3


@pytest.mark.asyncio
async def test_generated_artifact_mutations_invalidate_its_project_cache():
    artifact = artifact_row(project_id=33)
    service = make_service(uploads=[], artifacts=[])
    service._artifact_repo.get_any_by_public_id = AsyncMock(return_value=artifact)
    service._invalidate_project_cache = AsyncMock()

    await service.rename("art_test_plan", "新测试方案.docx", 7)
    await service.soft_delete("art_test_plan", 7)

    assert service._invalidate_project_cache.await_count == 2
    service._invalidate_project_cache.assert_awaited_with(artifact, 7)


@pytest.mark.asyncio
async def test_project_cache_invalidation_resolves_the_project_public_id(monkeypatch):
    artifact = artifact_row(project_id=33)
    service = make_service(uploads=[], artifacts=[])
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = "prj_assets"
    service._session.execute = AsyncMock(return_value=query_result)
    cache = MagicMock(invalidate=AsyncMock())
    monkeypatch.setattr("app.cache.domains.project_cache.get_project_cache", lambda: cache)

    await service._invalidate_project_cache(artifact, 7)

    cache.invalidate.assert_awaited_once_with(7, "prj_assets")


@pytest.mark.asyncio
async def test_library_can_store_a_direct_upload_without_creating_a_conversation(monkeypatch):
    service = make_service(uploads=[], artifacts=[])
    service._file_repo.create = AsyncMock(side_effect=lambda file_record: file_record)
    stored = SimpleNamespace(storage_path="uploads/7/library/readme.txt", file_name="readme.txt", file_size=5)
    monkeypatch.setattr(
        "app.services.library_service.local_storage.save_upload",
        AsyncMock(return_value=stored),
    )

    uploaded = await service.upload_file(b"hello", "readme.txt", 7, "text/plain")

    assert uploaded["source"] == "upload"
    assert uploaded["name"] == "readme.txt"
    created = service._file_repo.create.await_args.args[0]
    assert created.conversation_id is None
    assert created.storage_path == stored.storage_path
