from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import library as library_api


class _AssertingBackgroundTasks:
    def __init__(self, session):
        self._session = session
        self.calls: list[tuple[object, tuple[object, ...]]] = []

    def add_task(self, func, *args):
        assert self._session.commit.await_count == 1, "preview queue state must commit before the background task is registered"
        self.calls.append((func, args))


class _QueuedPreviewService:
    def __init__(self, _session):
        self.preview_job_required = True

    @staticmethod
    async def build_preview_job(_item_id: str, _user_id: int):
        return None

    async def get_preview(self, item_id: str, _user_id: int):
        return {
            "item": {"id": item_id},
            "viewer": "word",
            "preview_status": "queued",
        }


@pytest.mark.asyncio
async def test_preview_endpoint_commits_queue_before_registering_background_job(monkeypatch):
    monkeypatch.setattr(library_api, "DocumentPreviewService", _QueuedPreviewService)
    session = SimpleNamespace(commit=AsyncMock())
    tasks = _AssertingBackgroundTasks(session)

    await library_api.get_library_item_preview(
        background_tasks=tasks,
        item_id="art_preview",
        current_user=SimpleNamespace(internal_id=1),
        session=session,
    )

    session.commit.assert_awaited_once()
    assert len(tasks.calls) == 1


@pytest.mark.asyncio
async def test_docx_upload_commits_queue_before_registering_background_job(monkeypatch):
    class _LibraryService:
        def __init__(self, _session):
            pass

        async def upload_file(self, _content, _filename, _user_id, _content_type):
            return {"id": "file_preview", "extension": ".docx"}

    monkeypatch.setattr(library_api, "DocumentPreviewService", _QueuedPreviewService)
    monkeypatch.setattr(library_api, "LibraryService", _LibraryService)
    session = SimpleNamespace(commit=AsyncMock())
    tasks = _AssertingBackgroundTasks(session)
    file = SimpleNamespace(read=AsyncMock(return_value=b"docx"), filename="preview.docx", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    await library_api.upload_library_file(
        background_tasks=tasks,
        file=file,
        current_user=SimpleNamespace(internal_id=1),
        session=session,
    )

    session.commit.assert_awaited_once()
    assert len(tasks.calls) == 1
