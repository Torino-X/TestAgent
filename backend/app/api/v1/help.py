"""Markdown-backed Help Center API."""

from __future__ import annotations

import logging
from functools import lru_cache

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse

from app.core.response import error, success
from app.services.help_center_service import (
    HelpArticleNotFound,
    HelpAssetNotFound,
    HelpCenterService,
    HelpContentError,
    HelpSearchError,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@lru_cache(maxsize=1)
def get_help_center_service() -> HelpCenterService:
    return HelpCenterService()


def _content_failure(exc: HelpContentError) -> JSONResponse:
    logger.error("Help Center content failure | error=%s", exc)
    return JSONResponse(status_code=503, content=error(50310, "帮助内容暂时无法加载"))


@router.get("")
def get_help_catalog():
    try:
        return success(get_help_center_service().get_catalog().model_dump(mode="json"))
    except HelpContentError as exc:
        return _content_failure(exc)


@router.get("/search")
def search_help(
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=20, ge=1, le=50),
):
    try:
        result = get_help_center_service().search(q, limit)
        return success(result.model_dump(mode="json"))
    except HelpSearchError as exc:
        return JSONResponse(status_code=422, content=error(42230, str(exc)))
    except HelpContentError as exc:
        return _content_failure(exc)


@router.get("/articles/{slug}")
def get_help_article(slug: str):
    try:
        article = get_help_center_service().get_article(slug)
        return success(article.model_dump(mode="json"))
    except HelpArticleNotFound:
        return JSONResponse(status_code=404, content=error(40430, "未找到这篇帮助文档"))
    except HelpContentError as exc:
        return _content_failure(exc)


@router.get("/assets/{asset_path:path}")
def get_help_asset(asset_path: str):
    try:
        asset = get_help_center_service().resolve_asset(asset_path)
        return FileResponse(asset.path, media_type=asset.media_type)
    except HelpAssetNotFound:
        return JSONResponse(status_code=404, content=error(40431, "帮助资源不存在"))
    except HelpContentError as exc:
        return _content_failure(exc)

