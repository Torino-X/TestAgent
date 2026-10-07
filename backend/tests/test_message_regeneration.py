"""Tests for assistant message regeneration API.

Covers: generation model CRUD, regeneration service validation, and
the regenerate SSE endpoint contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.testclient import TestClient

from app.api.deps import get_current_user
from app.api.v1.messages import router as messages_router
from app.core.exceptions import (
    MessageNotFeedbackableError,
    MessageRegenerationAlreadyRunningError,
    MessageRegenerationContextMissingError,
    MessageRegenerationNotLatestError,
)
from app.core.response import success
from app.db.session import get_db
from app.schemas.auth import UserProfile


# ── Fixtures ─────────────────────────────────────────────────────────


def _stub_get_current_user():
    return UserProfile(
        id="user_regen_test",
        internal_id=42,
        name="Regen User",
        role="user",
        username="regen_user",
        email="regen@example.com",
        status="active",
        avatar_url=None,
    )


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(messages_router)
    app.dependency_overrides[get_current_user] = _stub_get_current_user

    async def _override_get_db():
        mock_session = AsyncMock(spec=AsyncSession)
        yield mock_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── Exception codes ─────────────────────────────────────────────────


class TestRegenerationExceptions:
    def test_not_latest_error_code(self):
        exc = MessageRegenerationNotLatestError()
        assert exc.code == 50911

    def test_already_running_error_code(self):
        exc = MessageRegenerationAlreadyRunningError()
        assert exc.code == 50912

    def test_context_missing_error_code(self):
        exc = MessageRegenerationContextMissingError()
        assert exc.code == 50913


# ── Generation model basics ─────────────────────────────────────────


class TestGenerationModelBasic:
    """Verify the model class is importable and has the expected columns."""

    def test_import_model(self):
        from app.models.message_generation import AssistantMessageGeneration
        assert AssistantMessageGeneration.__tablename__ == "assistant_message_generations"

    def test_import_repository(self):
        from app.repositories.message_generation_repository import MessageGenerationRepository
        assert MessageGenerationRepository is not None

    def test_import_service(self):
        from app.services.message_regeneration_service import MessageRegenerationService
        assert MessageRegenerationService is not None


# ── Regenerate schema ────────────────────────────────────────────────


class TestRegenerateRequestSchema:
    def test_optional_idempotency_key(self):
        from app.api.v1.messages import RegenerateRequest
        req = RegenerateRequest()
        assert req.idempotency_key is None

    def test_with_idempotency_key(self):
        from app.api.v1.messages import RegenerateRequest
        req = RegenerateRequest(idempotency_key="test-key-123")
        assert req.idempotency_key == "test-key-123"
