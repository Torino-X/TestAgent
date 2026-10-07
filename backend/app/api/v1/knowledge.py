"""Configuration and read-only query endpoints for the optional Company RAG."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import success
from app.db.session import get_db
from app.schemas.auth import UserProfile
from app.schemas.knowledge import (
    CompanyRagConfigPayload,
    CompanyRagConnectionTestResult,
    CompanyRagConfigPublic,
    CompanyRagSearchRequest,
    CompanyRagSearchResult,
)
from app.services.knowledge_config_service import CompanyRagConfigService
from app.services.knowledge_retrieval_service import KnowledgeRetrievalService

router = APIRouter()


@router.get("/config", response_model=dict)
async def get_company_rag_config(
    current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)
):
    data = await CompanyRagConfigService(session).get_config(current.internal_id)
    return success(CompanyRagConfigPublic(**data).model_dump())


@router.put("/config", response_model=dict)
async def save_company_rag_config(
    payload: CompanyRagConfigPayload,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    try:
        data = await CompanyRagConfigService(session).save_config(current.internal_id, payload.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return success(CompanyRagConfigPublic(**data).model_dump())


@router.post("/config/test", response_model=dict)
async def test_company_rag_connection(
    current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)
):
    data = await CompanyRagConfigService(session).test_connection(current.internal_id)
    return success(CompanyRagConnectionTestResult(**data).model_dump())


@router.post("/query", response_model=dict)
async def query_company_rag(
    payload: CompanyRagSearchRequest,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    result = await KnowledgeRetrievalService(session).retrieve(
        user_internal_id=current.internal_id,
        query=payload.query,
        top_k=payload.top_k,
        similarity_threshold=payload.similarity_threshold,
        retrieve_strategy=payload.retrieve_strategy,
    )
    if not result.success:
        raise HTTPException(status_code=502, detail={"code": result.error_code, "message": result.error_message})
    return success(CompanyRagSearchResult(
        hits=[{
            "id": hit.chunk_id or hit.doc_id or "",
            "title": hit.chunk_title or hit.doc_name or "",
            "content": hit.content,
            "score": hit.score,
            "source": hit.doc_name,
            "metadata": hit.metadata,
        } for hit in result.hits],
        hit_count=len(result.hits), elapsed_ms=result.elapsed_ms,
    ).model_dump())
