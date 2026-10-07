"""Configuration service for the optional Company RAG provider."""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoError, decrypt_api_key, encrypt_api_key, mask_api_key
from app.integrations.knowledge_base_client import (
    COMPANY_RAG_API_KEY_MISSING,
    COMPANY_RAG_CONFIG_MISSING,
    build_company_rag_client,
)
from app.repositories.knowledge_config_repository import KnowledgeConfigRepository
from app.utils.datetime import utcnow

logger = logging.getLogger(__name__)


class CompanyRagConfigService:
    """Store a masked, encrypted connection configuration; never manage KB data."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = KnowledgeConfigRepository(session)

    async def get_config(self, user_internal_id: int) -> dict:
        row = await self._repo.get_for_user(user_internal_id)
        if row is None:
            return self._unconfigured_public()
        return self._to_public(row)

    async def save_config(self, user_internal_id: int, data: dict) -> dict:
        existing = await self._repo.get_for_user(user_internal_id)
        api_base_url = str(data.get("api_base_url") or getattr(existing, "api_base_url", "") or "").strip().rstrip("/")
        self._validate_url(api_base_url)
        plain_api_key = str(data.get("api_key") or "").strip()
        encrypted = encrypt_api_key(plain_api_key) if plain_api_key else str(getattr(existing, "api_key_encrypted", "") or "")
        masked = mask_api_key(plain_api_key) if plain_api_key else str(getattr(existing, "api_key_masked", "") or "")
        if not encrypted:
            raise ValueError("Company RAG API key is required when saving a connection.")

        timeout_seconds = self._bounded_int(data.get("timeout_seconds"), default=30, low=3, high=120)
        top_k = self._bounded_int(data.get("top_k"), default=5, low=1, high=20)
        similarity_threshold = self._bounded_float(data.get("similarity_threshold"), default=0.35, low=0.0, high=1.0)
        retrieve_strategy = self._bounded_int(data.get("retrieve_strategy"), default=3, low=1, high=3)
        enabled = bool(data.get("enabled", True))
        saved = await self._repo.upsert_for_user(
            user_id=user_internal_id,
            public_id=existing.public_id if existing else None,
            api_base_url=api_base_url,
            api_key_encrypted=encrypted,
            api_key_masked=masked,
            default_knowledge_ids=[],
            top_k=top_k,
            similarity_threshold=similarity_threshold,
            retrieve_strategy=retrieve_strategy,
            enable_rerank_model=False,
            rerank_model=None,
            knowledge_graph=False,
            direct_answer_enabled=False,
            test_plan_generation_enabled=enabled,
            test_case_generation_enabled=False,
            timeout_seconds=timeout_seconds,
        )
        await self._session.flush()
        await self._session.refresh(saved)
        return self._to_public(saved)

    async def test_connection(self, user_internal_id: int) -> dict:
        row = await self._repo.get_for_user(user_internal_id)
        now = utcnow().isoformat()
        if row is None or not row.api_base_url:
            return self._test_failure(COMPANY_RAG_CONFIG_MISSING, "Company RAG is not configured.", now)
        try:
            api_key = decrypt_api_key(row.api_key_encrypted or "")
        except CryptoError:
            api_key = ""
        if not api_key:
            return self._test_failure(COMPANY_RAG_API_KEY_MISSING, "Company RAG API key is unavailable.", now)
        client = build_company_rag_client(api_base_url=row.api_base_url, api_key=api_key, timeout_seconds=int(row.timeout_seconds or 30))
        started = time.monotonic()
        try:
            result = await client.health_check()
        finally:
            await client.aclose()
        latency_ms = int((time.monotonic() - started) * 1000)
        await self._repo.update_test_status(user_id=user_internal_id, status="success" if result.success else "failed", message=result.error_message)
        return {
            "success": result.success,
            "latency_ms": latency_ms,
            "status": "success" if result.success else "failed",
            "message": "Connection succeeded." if result.success else (result.error_message or "Connection failed."),
            "error_code": result.error_code,
            "tested_at": now,
        }

    @staticmethod
    def _validate_url(value: str) -> None:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Company RAG API URL must be an absolute HTTP(S) URL.")

    @staticmethod
    def _bounded_int(value, *, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(value if value is not None else default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _bounded_float(value, *, default: float, low: float, high: float) -> float:
        try:
            return max(low, min(high, float(value if value is not None else default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _unconfigured_public() -> dict:
        return {"config_id": None, "enabled": False, "api_base_url": "", "api_key_masked": "", "api_key_set": False, "timeout_seconds": 30, "top_k": 5, "similarity_threshold": 0.35, "retrieve_strategy": 3, "last_test_status": None, "last_test_message": None, "updated_at": None}

    @staticmethod
    def _to_public(row) -> dict:
        return {"config_id": row.public_id, "enabled": bool(row.test_plan_generation_enabled), "api_base_url": row.api_base_url, "api_key_masked": row.api_key_masked or "", "api_key_set": bool(row.api_key_encrypted), "timeout_seconds": int(row.timeout_seconds or 30), "top_k": int(row.top_k or 5), "similarity_threshold": float(row.similarity_threshold or 0.35), "retrieve_strategy": int(row.retrieve_strategy or 3), "last_test_status": row.last_test_status, "last_test_message": row.last_test_message, "updated_at": (row.updated_at or utcnow()).isoformat()}

    @staticmethod
    def _test_failure(code: str, message: str, tested_at: str) -> dict:
        return {"success": False, "latency_ms": 0, "status": "failed", "message": message, "error_code": code, "tested_at": tested_at}


# Kept as an import-compatible alias while callers are migrated.
KnowledgeConfigService = CompanyRagConfigService
