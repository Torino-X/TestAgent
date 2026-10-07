from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from tests.help_test_support import write_help_fixture


@pytest.mark.asyncio
async def test_help_api_is_mounted_under_the_application_api_prefix():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        mounted = await client.get("/api/help")
        incorrect_v1_path = await client.get("/api/v1/help")

    assert mounted.status_code == 200
    assert mounted.json()["data"]["site"]["title"] == "TestAgent 帮助中心"
    assert incorrect_v1_path.status_code == 404


@pytest.mark.asyncio
async def test_help_api_catalog_article_search_asset_and_404(tmp_path, monkeypatch):
    from app.api.v1.help import router
    from app.services.help_center_service import HelpCenterService

    service = HelpCenterService(write_help_fixture(tmp_path))
    monkeypatch.setattr("app.api.v1.help.get_help_center_service", lambda: service)

    app = FastAPI()
    app.include_router(router, prefix="/api/help")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        catalog = await client.get("/api/help")
        article = await client.get("/api/help/articles/quick-start")
        search = await client.get("/api/help/search", params={"q": "模板"})
        asset = await client.get("/api/help/assets/diagram.png")
        missing = await client.get("/api/help/articles/missing")

    assert catalog.status_code == 200
    assert catalog.json()["data"]["sections"][0]["articles"][0]["slug"] == "quick-start"
    assert article.status_code == 200
    assert article.json()["data"]["headings"][0]["anchor"] == "准备文件"
    assert search.status_code == 200
    assert search.json()["data"]["items"][0]["slug"] == "template-driven"
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("image/png")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_help_api_rejects_overlong_search_query(tmp_path, monkeypatch):
    from app.api.v1.help import router
    from app.services.help_center_service import HelpCenterService

    service = HelpCenterService(write_help_fixture(tmp_path))
    monkeypatch.setattr("app.api.v1.help.get_help_center_service", lambda: service)
    app = FastAPI()
    app.include_router(router, prefix="/api/help")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/help/search", params={"q": "模" * 101})
    assert response.status_code == 422
