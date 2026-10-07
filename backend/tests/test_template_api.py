from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.api.deps import get_current_user
from app.api.v1.templates import router
from app.core.exceptions import AppError
from app.core.response import error
from app.db.session import get_db
from app.models.agent_task import AgentTask
from app.models.message import Message
from app.models.uploaded_file import UploadedFile
from app.models.user import User
from app.schemas.auth import UserProfile
from tests.test_template_service import InMemoryTemplateStorage, _docx_bytes


def _status_for_error(code: int) -> int:
    if 40100 <= code < 40200:
        return 401
    if 40300 <= code < 40400:
        return 403
    if 40400 <= code < 40500:
        return 404
    if 40900 <= code < 41000:
        return 409
    if 41300 <= code < 41400:
        return 413
    if 42200 <= code < 42300:
        return 422
    return 500


def _api_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/templates")

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError):
        return JSONResponse(
            status_code=_status_for_error(exc.code),
            content=error(exc.code, exc.message, exc.detail),
        )

    return app


@pytest.mark.asyncio
async def test_template_api_requires_authentication():
    app = _api_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/templates/market")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_template_http_upload_market_save_download_use_and_delete_contract(
    sqlite_session_factory,
    monkeypatch,
):
    storage = InMemoryTemplateStorage()
    monkeypatch.setattr("app.services.template_service.object_storage", storage)
    monkeypatch.setattr("app.services.template_use_service.object_storage", storage)
    monkeypatch.setattr("app.services.template_preview_service.object_storage", storage)
    monkeypatch.setattr(
        "app.services.document_preview_service.DocumentPreviewService._docx_to_pdf",
        lambda _service, _content: b"%PDF-1.4\n% api template cover\n",
    )

    async with sqlite_session_factory() as session:
        now = datetime(2026, 9, 15, 12, 0, 0)
        session.add_all([
            User(
                id=1, public_id="user_owner", username="owner", password_hash="test",
                role="user", status="active", created_at=now, updated_at=now,
            ),
            User(
                id=2, public_id="user_reader", username="reader", password_hash="test",
                role="user", status="active", created_at=now, updated_at=now,
            ),
        ])
        await session.commit()

        app = _api_app()
        active_user = {"id": 1, "public_id": "user_owner", "name": "Owner"}

        async def current_user_override():
            return UserProfile(
                id=active_user["public_id"],
                internal_id=active_user["id"],
                name=active_user["name"],
                role="user",
            )

        async def db_override():
            yield session

        app.dependency_overrides[get_current_user] = current_user_override
        app.dependency_overrides[get_db] = db_override

        content = _docx_bytes()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            uploaded_response = await client.post(
                "/api/templates/mine",
                data={
                    "name": "HTTP 测试方案",
                    "category_code": "test_plan",
                    "description": "API contract",
                    "tags_json": '["Web", "通用"]',
                    "publish": "true",
                },
                files={
                    "file": (
                        "HTTP测试方案.docx",
                        content,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
            assert uploaded_response.status_code == 200
            uploaded = uploaded_response.json()["data"]
            assert uploaded["source_type"] == "owner"
            assert uploaded["cover"]["status"] == "ready"
            assert uploaded["cover"]["kind"] == "pdf"
            assert "storage_path" not in uploaded

            market_response = await client.get(
                "/api/templates/market",
                params={"q": "HTTP", "category": "test_plan", "sort": "newest"},
            )
            assert market_response.status_code == 200
            assert market_response.json()["data"]["total"] == 1

            preview = await client.get(
                f"/api/templates/market/{uploaded['template_id']}/preview"
            )
            assert preview.status_code == 200
            assert preview.json()["data"]["viewer"] == "word"
            first_cover = await client.get(preview.json()["data"]["pdf_url"])
            second_cover = await client.get(preview.json()["data"]["pdf_url"])
            assert first_cover.content == second_cover.content
            assert first_cover.content.startswith(b"%PDF")

            mine_preview = await client.get(
                f"/api/templates/mine/{uploaded['id']}/preview"
            )
            assert mine_preview.status_code == 200
            assert mine_preview.json()["data"]["pdf_url"] == (
                f"/api/templates/mine/{uploaded['id']}/preview/pdf"
            )

            active_user.update(id=2, public_id="user_reader", name="Reader")
            forbidden_private_cover = await client.get(
                f"/api/templates/mine/{uploaded['id']}/preview/pdf"
            )
            assert forbidden_private_cover.status_code == 403
            first_save = await client.post(
                f"/api/templates/market/{uploaded['template_id']}/save"
            )
            second_save = await client.post(
                f"/api/templates/market/{uploaded['template_id']}/save"
            )
            assert first_save.status_code == 200
            assert first_save.json()["data"]["already_saved"] is False
            assert second_save.json()["data"]["already_saved"] is True
            saved_id = first_save.json()["data"]["user_template"]["id"]

            forbidden_publish = await client.post(
                f"/api/templates/mine/{saved_id}/publish"
            )
            assert forbidden_publish.status_code == 403

            mine = await client.get("/api/templates/mine", params={"source_type": "market_saved"})
            assert mine.json()["data"]["total"] == 1

            downloaded = await client.get(f"/api/templates/mine/{saved_id}/download")
            assert downloaded.status_code == 200
            assert downloaded.content == content
            assert "attachment" in downloaded.headers["content-disposition"]

            used = await client.post(f"/api/templates/mine/{saved_id}/use", json={})
            assert used.status_code == 200
            assert used.json()["data"]["conversation"]["id"].startswith("conv_")
            assert used.json()["data"]["uploaded_file"]["id"].startswith("file_")

            removed = await client.delete(f"/api/templates/mine/{saved_id}")
            assert removed.status_code == 200
            assert (await client.get("/api/templates/mine")).json()["data"]["total"] == 0

            active_user.update(id=1, public_id="user_owner", name="Owner")
            missing = await client.get("/api/templates/market/tpl_missing/preview")
            assert missing.status_code == 404
            invalid_upload = await client.post(
                "/api/templates/mine",
                data={"name": "不支持", "category_code": "test_plan"},
                files={"file": ("unsafe.txt", b"plain", "text/plain")},
            )
            assert invalid_upload.status_code == 422
            conflict = await client.delete(f"/api/templates/mine/{uploaded['id']}")
            assert conflict.status_code == 409
            await client.post(f"/api/templates/mine/{uploaded['id']}/unpublish")
            deleted = await client.delete(f"/api/templates/mine/{uploaded['id']}")
            assert deleted.status_code == 200

        assert await session.scalar(select(func.count(UploadedFile.id))) == 1
        assert await session.scalar(select(func.count(Message.id))) == 0
        assert await session.scalar(select(func.count(AgentTask.id))) == 0
