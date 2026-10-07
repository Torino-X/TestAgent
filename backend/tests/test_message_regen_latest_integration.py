"""Phase 2.9A.37 — integration tests for regeneration latest-message validation.

Runs against a real in-memory SQLite database (see ``tests/conftest.py``)
so the actual SQL ordering / LIMIT semantics used by
``MessageRegenerationService.validate_and_prepare`` are exercised.

The headline regression: two messages written in the same ``created_at``
second (which is exactly what ``MessageService.send_message`` produces)
must still order the agent reply *after* its triggering user message,
because ``conversation_sequence`` / ``id`` are the authoritative stable
sort keys — not the second-resolution ``created_at``.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core.exceptions import (
    MessageNotFeedbackableError,
    MessageRegenerationContextMissingError,
    MessageRegenerationNotLatestError,
    NotFoundError,
)
from app.main import _http_status_for_app_error
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.repositories.message_generation_repository import MessageGenerationRepository
from app.services.message_regeneration_service import MessageRegenerationService

_SAME_SECOND = datetime(2026, 8, 1, 8, 20, 4)


async def _seed_conversation(session_factory, *, messages, user_id=1, conv_id=100):
    """Insert a user + conversation + the given messages."""
    session = session_factory()
    async with session:
        user = User(id=user_id, public_id=f"user_{user_id}", username=f"u{user_id}", password_hash="x")
        session.add(user)
        await session.flush()
        conv = Conversation(id=conv_id, public_id=f"conv_{conv_id}", user_id=user_id)
        session.add(conv)
        await session.flush()
        for msg in messages:
            created = msg.get("created_at", _SAME_SECOND)
            session.add(
                Message(
                    id=msg["id"],
                    public_id=msg["public_id"],
                    user_id=msg.get("user_id", user_id),
                    conversation_id=msg.get("conversation_id", conv.id),
                    role=msg["role"],
                    message_type=msg["message_type"],
                    content=msg.get("content", ""),
                    conversation_sequence=msg.get("conversation_sequence"),
                    created_at=created,
                    updated_at=created,
                    deleted_at=msg.get("deleted_at"),
                    reply_to_message_id=msg.get("reply_to_message_id"),
                )
            )
        await session.commit()
    await session.close()


def _service(session) -> MessageRegenerationService:
    return MessageRegenerationService(
        session,
        generation_repo=MessageGenerationRepository(session),
    )


async def _prepare(session_factory, public_id, user_id=1, conv_public_id="conv_100", commit=True):
    session = session_factory()
    try:
        result = await _service(session).validate_and_prepare(
            user_id=user_id,
            conversation_public_id=conv_public_id,
            message_public_id=public_id,
        )
        if commit:
            await session.commit()
        return result
    finally:
        await session.close()


class TestLatestMessageStableOrdering:
    """Stable ordering: conversation_sequence / id are authoritative."""

    @pytest.mark.asyncio
    async def test_same_created_at_allows_agent_reload(self, sqlite_session_factory):
        """[Matrix #1][#2] User seq=1 and Agent seq=2 share created_at and
        adjacent ids → Agent is the latest user-visible message and must be
        allowed to regenerate (conversation_sequence picks the agent)."""
        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {
                    "id": 149,
                    "public_id": "msg_user",
                    "role": "user",
                    "message_type": "user_text",
                    "content": "你好",
                    "conversation_sequence": 1,
                },
                {
                    "id": 150,
                    "public_id": "msg_agent",
                    "role": "agent",
                    "message_type": "agent_text",
                    "content": "你好，我是 TestAgent 助手……",
                    "conversation_sequence": 2,
                    "reply_to_message_id": 149,
                },
            ],
        )
        result = await _prepare(sqlite_session_factory, "msg_agent")
        assert result[0] == 150
        assert result[2] == "你好"
        assert result[4] == "msg_agent"

    @pytest.mark.asyncio
    async def test_returns_conversation_public_id(self, sqlite_session_factory):
        """[Regression] The SSE endpoint keys ``stream_message`` on the
        conversation *public* id.  The service must return ``conv_100`` (the
        public id), not the internal integer ``100`` — returning the internal
        id would make the downstream public-id lookup fail with
        NotFoundError("会话")."""
        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                {"id": 150, "public_id": "msg_agent", "role": "agent", "message_type": "agent_text", "content": "回复", "conversation_sequence": 2, "reply_to_message_id": 149},
            ],
        )
        result = await _prepare(sqlite_session_factory, "msg_agent")
        assert result[1] == "conv_100"

    @pytest.mark.asyncio
    async def test_later_user_message_rejects_reload(self, sqlite_session_factory):
        """[Matrix #4] User seq=3 after the agent reply → rejected."""
        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user1", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                {"id": 150, "public_id": "msg_agent", "role": "agent", "message_type": "agent_text", "content": "回复1", "conversation_sequence": 2, "reply_to_message_id": 149},
                {"id": 151, "public_id": "msg_user2", "role": "user", "message_type": "user_text", "content": "继续", "conversation_sequence": 3, "created_at": datetime(2026, 8, 1, 8, 20, 5)},
            ],
        )
        with pytest.raises(MessageRegenerationNotLatestError):
            await _prepare(sqlite_session_factory, "msg_agent")

    @pytest.mark.asyncio
    async def test_later_agent_rejects_older_and_allows_newer(self, sqlite_session_factory):
        """[Matrix #5] Agent seq=2 then Agent seq=3 → seq=2 rejected, seq=3 allowed."""
        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                {"id": 150, "public_id": "msg_agent2", "role": "agent", "message_type": "agent_text", "content": "回复1", "conversation_sequence": 2, "reply_to_message_id": 149},
                {"id": 152, "public_id": "msg_agent3", "role": "agent", "message_type": "agent_text", "content": "回复2", "conversation_sequence": 3, "reply_to_message_id": 149, "created_at": datetime(2026, 8, 1, 8, 20, 5)},
            ],
        )
        with pytest.raises(MessageRegenerationNotLatestError):
            await _prepare(sqlite_session_factory, "msg_agent2")
        result = await _prepare(sqlite_session_factory, "msg_agent3")
        assert result[0] == 152

    @pytest.mark.asyncio
    async def test_same_sequence_uses_id_desc(self, sqlite_session_factory):
        """[Matrix #3] Two rows with the same conversation_sequence →
        ``id DESC`` breaks the tie deterministically."""
        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                # seq=2 deliberately duplicated; higher id must win
                {"id": 150, "public_id": "msg_agent_a", "role": "agent", "message_type": "agent_text", "content": "回复A", "conversation_sequence": 2, "reply_to_message_id": 149},
                {"id": 160, "public_id": "msg_agent_b", "role": "agent", "message_type": "agent_text", "content": "回复B", "conversation_sequence": 2, "reply_to_message_id": 149},
            ],
        )
        result = await _prepare(sqlite_session_factory, "msg_agent_b")
        assert result[0] == 160
        with pytest.raises(MessageRegenerationNotLatestError):
            await _prepare(sqlite_session_factory, "msg_agent_a")


class TestTargetMessageValidation:
    """Business rules for the target message itself."""

    @pytest.mark.asyncio
    async def test_target_user_message_rejected(self, sqlite_session_factory):
        """[Matrix #6] Target role=user → rejected as non-feedbackable."""
        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                {"id": 150, "public_id": "msg_agent", "role": "agent", "message_type": "agent_text", "content": "回复", "conversation_sequence": 2, "reply_to_message_id": 149},
            ],
        )
        with pytest.raises(MessageNotFeedbackableError):
            await _prepare(sqlite_session_factory, "msg_user")

    @pytest.mark.asyncio
    async def test_soft_deleted_target_not_found(self, sqlite_session_factory):
        """[Matrix #7] Target soft-deleted → NotFoundError (project convention:
        deleted rows are invisible to the caller)."""
        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                {"id": 150, "public_id": "msg_agent", "role": "agent", "message_type": "agent_text", "content": "回复", "conversation_sequence": 2, "reply_to_message_id": 149, "deleted_at": datetime(2026, 8, 1, 8, 20, 6)},
            ],
        )
        with pytest.raises(NotFoundError):
            await _prepare(sqlite_session_factory, "msg_agent")

    @pytest.mark.asyncio
    async def test_latest_soft_deleted_does_not_block_previous(self, sqlite_session_factory):
        """[Matrix #8] A soft-deleted trailing row must not block the previous
        valid agent reply from being the latest user-visible message."""
        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                {"id": 150, "public_id": "msg_agent", "role": "agent", "message_type": "agent_text", "content": "回复", "conversation_sequence": 2, "reply_to_message_id": 149},
                # a deleted agent row after it
                {"id": 153, "public_id": "msg_deleted_agent", "role": "agent", "message_type": "agent_text", "content": "已删除", "conversation_sequence": 3, "deleted_at": datetime(2026, 8, 1, 8, 20, 5)},
            ],
        )
        result = await _prepare(sqlite_session_factory, "msg_agent")
        assert result[0] == 150

    @pytest.mark.asyncio
    async def test_other_conversation_does_not_affect(self, sqlite_session_factory):
        """[Matrix #9] A newer message in another conversation must not block."""
        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                {"id": 150, "public_id": "msg_agent", "role": "agent", "message_type": "agent_text", "content": "回复", "conversation_sequence": 2, "reply_to_message_id": 149},
            ],
            conv_id=100,
        )
        # a newer message in a different conversation (conv_id=999), owned by
        # user 1 — but user 1 already exists from the first seed, so reuse it
        # without re-inserting by adding just the conversation + message.
        session = sqlite_session_factory()
        async with session:
            conv = Conversation(id=999, public_id="conv_999", user_id=1)
            session.add(conv)
            await session.flush()
            session.add(
                Message(
                    id=170, public_id="msg_other_conv", user_id=1,
                    conversation_id=999, role="user", message_type="user_text",
                    content="别的会话", conversation_sequence=1,
                    created_at=datetime(2026, 8, 1, 8, 20, 6),
                    updated_at=datetime(2026, 8, 1, 8, 20, 6),
                )
            )
            await session.commit()
        await session.close()
        result = await _prepare(sqlite_session_factory, "msg_agent", conv_public_id="conv_100")
        assert result[0] == 150

    @pytest.mark.asyncio
    async def test_other_user_message_does_not_affect(self, sqlite_session_factory):
        """[Matrix #10] A newer message owned by another user must not block."""
        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                {"id": 150, "public_id": "msg_agent", "role": "agent", "message_type": "agent_text", "content": "回复", "conversation_sequence": 2, "reply_to_message_id": 149},
            ],
        )
        # message owned by user 2 in the same conversation, seq=3
        session = sqlite_session_factory()
        async with session:
            from sqlalchemy import select as _sa_select
            conv_id = (await session.execute(_sa_select(Conversation.id).where(Conversation.public_id == "conv_100"))).scalar_one()
            session.add(
                Message(
                    id=180, public_id="msg_other_user", user_id=2, conversation_id=conv_id,
                    role="user", message_type="user_text", content="别人的消息",
                    conversation_sequence=3, created_at=datetime(2026, 8, 1, 8, 20, 5),
                    updated_at=datetime(2026, 8, 1, 8, 20, 5),
                )
            )
            await session.commit()
        await session.close()
        result = await _prepare(sqlite_session_factory, "msg_agent")
        assert result[0] == 150

    @pytest.mark.asyncio
    async def test_public_id_resolves_to_internal(self, sqlite_session_factory):
        """[Matrix #11] target public_id correctly resolves to internal id."""
        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                {"id": 150, "public_id": "msg_agent", "role": "agent", "message_type": "agent_text", "content": "回复", "conversation_sequence": 2, "reply_to_message_id": 149},
            ],
        )
        result = await _prepare(sqlite_session_factory, "msg_agent")
        assert result[0] == 150
        assert result[4] == "msg_agent"


class TestValidationBeforeGeneration:
    """Validation must happen before any generation record is created."""

    @pytest.mark.asyncio
    async def test_no_generation_created_on_rejection(self, sqlite_session_factory):
        """[Matrix #12] A rejected regenerate must not leave a generation row."""
        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user1", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                {"id": 150, "public_id": "msg_agent", "role": "agent", "message_type": "agent_text", "content": "回复1", "conversation_sequence": 2, "reply_to_message_id": 149},
                {"id": 151, "public_id": "msg_user2", "role": "user", "message_type": "user_text", "content": "继续", "conversation_sequence": 3},
            ],
        )
        with pytest.raises(MessageRegenerationNotLatestError):
            await _prepare(sqlite_session_factory, "msg_agent")
        session = sqlite_session_factory()
        try:
            from sqlalchemy import text as _t
            count = (await session.execute(_t("SELECT COUNT(*) FROM assistant_message_generations"))).scalar_one()
            assert count == 0
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_success_creates_one_generation(self, sqlite_session_factory):
        """[Matrix #13] Success path returns target + context and creates a
        pending generation."""
        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                {"id": 150, "public_id": "msg_agent", "role": "agent", "message_type": "agent_text", "content": "回复", "conversation_sequence": 2, "reply_to_message_id": 149},
            ],
        )
        result = await _prepare(sqlite_session_factory, "msg_agent")
        assert result[0] == 150
        assert result[2] == "你好"
        session = sqlite_session_factory()
        try:
            from sqlalchemy import text as _t
            row = (await session.execute(
                _t("SELECT message_id, status, is_active FROM assistant_message_generations WHERE public_id = :pid"),
                {"pid": result[3]},
            )).first()
            assert row is not None
            assert row[0] == 150
            assert row[1] == "running"
            assert row[2] == 1
        finally:
            await session.close()

    @pytest.mark.asyncio
    async def test_legacy_no_reply_anchor_falls_back_to_latest_user(self, sqlite_session_factory):
        """A legacy agent row without reply_to_message_id still resolves its
        original prompt via the stable ordering fallback."""
        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                {"id": 150, "public_id": "msg_agent", "role": "agent", "message_type": "agent_text", "content": "回复", "conversation_sequence": 2, "reply_to_message_id": None},
            ],
        )
        result = await _prepare(sqlite_session_factory, "msg_agent")
        assert result[2] == "你好"


class TestRegenerationHttpStatusMapping:
    """50911 is a business conflict, not a server error — must map to 409."""

    def test_not_latest_maps_to_409(self):
        assert _http_status_for_app_error(50911) == 409

    def test_other_regen_errors_map_to_409(self):
        # 50901 non-feedbackable, 50912 already running, 50913 context missing
        for code in (50901, 50912, 50913):
            assert _http_status_for_app_error(code) == 409

    def test_server_error_range_unchanged(self):
        assert _http_status_for_app_error(50001) == 500
        assert _http_status_for_app_error(50999 + 1) == 500

    def test_agent_runtime_unavailable_maps_to_503(self):
        # 504xx are application runtime-readiness errors, not uncaught
        # server crashes. Clients should be able to distinguish retryable
        # infrastructure unavailability from a generic 500.
        assert _http_status_for_app_error(50403) == 503


# ── API-level integration: full HTTP path against real SQLite ────────


class TestRegenerateApi:
    """POST /api/messages/{id}/regenerate end-to-end (real DB + real handler).

    Uses the production error handler so the HTTP status comes from
    ``_http_status_for_app_error`` exactly as in ``create_app``.
    """

    async def _make_app(self, sqlite_session_factory):
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse
        from sqlalchemy.ext.asyncio import AsyncSession

        from app.api.deps import get_current_user
        from app.api.v1.messages import router as messages_router
        from app.core.exceptions import AppError
        from app.core.response import error as error_response
        from app.db.session import get_db
        from app.main import _http_status_for_app_error
        from app.schemas.auth import UserProfile

        app = FastAPI()
        app.include_router(messages_router, prefix="/api")

        def _current_user():
            return UserProfile(
                id="user_api",
                internal_id=1,
                name="API User",
                role="user",
                username="api_user",
                status="active",
            )

        app.dependency_overrides[get_current_user] = _current_user

        async def _override_get_db():
            async with sqlite_session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = _override_get_db

        @app.exception_handler(AppError)
        async def _handler(_request: Request, exc: AppError):
            http_status = _http_status_for_app_error(exc.code)
            return JSONResponse(
                content=error_response(exc.code, exc.message, exc.detail),
                status_code=http_status,
            )

        return app

    @pytest.mark.asyncio
    async def test_later_user_message_returns_409(self, sqlite_session_factory):
        """[Matrix #14] Agent seq=2 followed by User seq=3 → HTTP 409 + code 50911."""
        from starlette.testclient import TestClient

        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user1", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                {"id": 150, "public_id": "msg_agent", "role": "agent", "message_type": "agent_text", "content": "回复1", "conversation_sequence": 2, "reply_to_message_id": 149},
                {"id": 151, "public_id": "msg_user2", "role": "user", "message_type": "user_text", "content": "继续", "conversation_sequence": 3},
            ],
        )
        app = await self._make_app(sqlite_session_factory)
        with TestClient(app) as client:
            resp = client.post("/api/messages/msg_agent/regenerate", json={})
            assert resp.status_code == 409
            body = resp.json()
            assert body["code"] == 50911
            assert body["message"] == "只能重新生成当前会话的最新回复"
            assert body["data"] is None

    @pytest.mark.asyncio
    async def test_non_agent_target_returns_business_error(self, sqlite_session_factory):
        """[Matrix #C] Target is a user_text message → business error, not 500."""
        from starlette.testclient import TestClient

        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
            ],
        )
        app = await self._make_app(sqlite_session_factory)
        with TestClient(app) as client:
            resp = client.post("/api/messages/msg_user/regenerate", json={})
            assert resp.status_code == 409
            body = resp.json()
            assert body["code"] == 50901
            assert "Agent" in body["message"]

    @pytest.mark.asyncio
    async def test_success_path_does_not_return_50911(self, sqlite_session_factory):
        """[Matrix #15] A valid regenerate starts the SSE stream — no 50911,
        no 500.  The SSE body is inspected before the stream closes."""
        from starlette.testclient import TestClient

        await _seed_conversation(
            sqlite_session_factory,
            messages=[
                {"id": 149, "public_id": "msg_user", "role": "user", "message_type": "user_text", "content": "你好", "conversation_sequence": 1},
                {"id": 150, "public_id": "msg_agent", "role": "agent", "message_type": "agent_text", "content": "回复", "conversation_sequence": 2, "reply_to_message_id": 149},
            ],
        )
        app = await self._make_app(sqlite_session_factory)
        with TestClient(app) as client:
            # The endpoint reaches StreamingResponse before the first token;
            # an early validation error (50911) would surface as a JSON body
            # with that code instead.  Just assert the request does not
            # short-circuit into the 409 envelope.
            resp = client.post("/api/messages/msg_agent/regenerate", json={})
            assert resp.status_code == 200
            body = resp.text
            assert "50911" not in body
            assert "只能重新生成" not in body
