"""HTTP contract tests for the Phase 3 Project API."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.api.v1.projects import conversation_router, router
from app.core.exceptions import AppError
from app.core.response import error
from app.db.session import get_db
from app.models.uploaded_file import UploadedFile
from app.models.user import User
from app.schemas.auth import UserProfile


@pytest.mark.asyncio
async def test_project_http_crud_relations_and_unified_envelope(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        now = datetime(2026, 9, 8, 9, 0, 0)
        session.add(User(
            id=1,
            public_id="user_api",
            username="user_api",
            password_hash="test",
            role="user",
            status="active",
            created_at=now,
            updated_at=now,
        ))
        session.add(UploadedFile(
            id=10,
            public_id="file_api",
            user_id=1,
            conversation_id=None,
            original_name="接口规范.md",
            stored_name="api.md",
            file_ext="md",
            file_size=10,
            file_type="document",
            upload_status="uploaded",
            storage_type="local",
            storage_path="safe/api.md",
            created_at=now,
            updated_at=now,
        ))
        await session.commit()

        app = FastAPI()
        # Match the production application prefix (`settings.api_prefix == "/api"`).
        app.include_router(router, prefix="/api/projects")
        app.include_router(conversation_router, prefix="/api")

        @app.exception_handler(AppError)
        async def handle_app_error(_request: Request, exc: AppError):
            status = 404 if 40400 <= exc.code < 40500 else 400
            return JSONResponse(status_code=status, content=error(exc.code, exc.message, exc.detail))

        async def current_user_override():
            return UserProfile(id="user_api", internal_id=1, name="API User", role="user")

        async def db_override():
            yield session

        app.dependency_overrides[get_current_user] = current_user_override
        app.dependency_overrides[get_db] = db_override

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created_response = await client.post("/api/projects", json={
                "name": "API Project",
                "description": "HTTP contract",
                "memoryMode": "project_memory",
            })
            assert created_response.status_code == 200
            created_body = created_response.json()
            assert created_body["code"] == 0
            project_id = created_body["data"]["id"]
            assert project_id.startswith("prj_")

            list_body = (await client.get("/api/projects", params={"q": "API", "scope": "all"})).json()
            assert list_body["data"]["total"] == 1

            updated = (await client.patch(f"/api/projects/{project_id}", json={
                "description": "Phase 5 integrated",
                "memoryMode": "none",
            })).json()["data"]
            assert updated["description"] == "Phase 5 integrated"
            assert updated["memoryMode"] == "none"

            pinned = (await client.post(f"/api/projects/{project_id}/pin")).json()["data"]
            assert pinned["pinned"] is True
            unpinned = (await client.delete(f"/api/projects/{project_id}/pin")).json()["data"]
            assert unpinned["pinned"] is False

            conversation_body = (await client.post(
                f"/api/projects/{project_id}/conversations",
                json={"title": "项目对话"},
            )).json()
            conversation_id = conversation_body["data"]["id"]

            source_body = (await client.post(
                f"/api/projects/{project_id}/sources/attach",
                json={"fileId": "file_api", "sourceRole": "api"},
            )).json()
            assert source_body["data"]["sourceRole"] == "api_spec"
            source_id = source_body["data"]["id"]
            source_list = (await client.get(f"/api/projects/{project_id}/sources")).json()["data"]
            assert source_list["total"] == 1
            assert source_list["items"][0]["fileId"] == "file_api"

            instructions = await client.put(
                f"/api/projects/{project_id}/instructions",
                json={"instructions": "遵循项目接口规范。"},
            )
            assert instructions.json()["data"]["instructions"] == "遵循项目接口规范。"
            assert (await client.get(f"/api/projects/{project_id}/instructions")).json()["data"] == {
                "instructions": "遵循项目接口规范。"
            }

            knowledge = await client.put(
                f"/api/projects/{project_id}/knowledge-connections",
                json={"knowledgeIds": ["kb_api"]},
            )
            assert knowledge.status_code == 404

            conversations = (await client.get(f"/api/projects/{project_id}/conversations")).json()["data"]
            assert conversations["total"] == 1
            assert conversations["items"][0]["id"] == conversation_id
            assert (await client.get(f"/api/projects/{project_id}/artifacts")).json()["data"] == {
                "items": [], "total": 0
            }

            removed = await client.delete(f"/api/conversations/{conversation_id}/project")
            assert removed.json()["code"] == 0

            moved = await client.post(
                f"/api/conversations/{conversation_id}/project",
                json={"projectId": project_id},
            )
            assert moved.json()["data"]["id"] == conversation_id

            source_removed = await client.delete(f"/api/projects/{project_id}/sources/{source_id}")
            assert source_removed.json()["code"] == 0

            deleted = await client.delete(f"/api/projects/{project_id}")
            assert deleted.json()["message"] == "项目已删除"
            missing = await client.get(f"/api/projects/{project_id}")
            assert missing.status_code == 404
            assert missing.json()["code"] == 40401
