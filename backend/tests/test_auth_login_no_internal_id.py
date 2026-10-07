"""Verify that internal integer DB ids never reach API responses.

These checks cover the three places a UserProfile could be serialized:
- LoginResponse (POST /api/auth/login)
- explicit model_dump_json() on UserProfile
- AuthTokenResponse used by /auth/register and /auth/profile
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.security import hash_password
from app.schemas.auth import (
    AuthTokenResponse,
    LoginResponse,
    UserProfile,
    UserResponse,
)
from app.services.auth_service import AuthService


class DummySession:
    async def flush(self) -> None:
        pass


class FakeUserRepository:
    def __init__(self, user) -> None:
        self.user = user

    async def get_by_username(self, username):
        return self.user if username == self.user.username else None

    async def get_by_email(self, email):
        return self.user if email == self.user.email else None


def _make_user(internal_id: int = 99) -> SimpleNamespace:
    return SimpleNamespace(
        id=internal_id,
        public_id="user_public_abc",
        username="alice",
        email="alice@example.com",
        password_hash=hash_password("pw-strong-123"),
        display_name="Alice",
        role="user",
        status="active",
        avatar_url=None,
        last_login_at=None,
    )


def _make_service(user) -> AuthService:
    svc = AuthService.__new__(AuthService)
    svc._session = DummySession()
    svc._user_repo = FakeUserRepository(user)
    return svc


@pytest.mark.asyncio
async def test_login_response_omits_internal_id():
    """The /auth/login response JSON must not include the integer DB id."""
    svc = _make_service(_make_user(internal_id=42))
    result = await svc.login("alice", "pw-strong-123")

    # Sanity: it really is a LoginResponse with a UserResponse user.
    assert isinstance(result, LoginResponse)
    assert isinstance(result.user, UserResponse)

    payload = json.loads(result.model_dump_json())
    user_section = payload["user"]
    assert "internal_id" not in user_section
    assert "id" not in user_section  # UserResponse uses user_id, not id
    assert user_section["user_id"] == "user_public_abc"


@pytest.mark.asyncio
async def test_user_profile_model_dump_json_omits_internal_id():
    """Even when something serializes UserProfile directly, internal_id
    must be excluded via the schema's ``exclude=True`` field marker."""
    profile = UserProfile(
        id="user_public_abc",
        internal_id=42,
        name="Alice",
        role="user",
        username="alice",
        email="alice@example.com",
    )
    payload = json.loads(profile.model_dump_json())
    assert "internal_id" not in payload
    # Sanity: other fields are still present
    assert payload["id"] == "user_public_abc"
    assert payload["name"] == "Alice"


def test_auth_token_response_shape_does_not_include_internal_id():
    """AuthTokenResponse (used by /register, /update) must also not
    expose internal_id — verified by checking its declared fields."""
    fields = set(AuthTokenResponse.model_fields.keys())
    assert "internal_id" not in fields
    # Build a minimal valid AuthTokenResponse — purely structural check.
    resp = AuthTokenResponse(
        access_token="t",
        user=UserResponse(
            user_id="u1",
            username="alice",
            display_name=None,
            email=None,
            role="user",
            status="active",
            avatar_url=None,
        ),
    )
    payload = json.loads(resp.model_dump_json())
    assert "internal_id" not in payload["user"]


@pytest.mark.asyncio
async def test_user_profile_internal_id_is_still_accessible_in_process():
    """Sanity: although internal_id is excluded from JSON, in-process
    code that needs the integer for DB ops must still be able to read it
    via attribute access — the existing route/service contract depends
    on this."""
    profile = UserProfile(
        id="user_public_abc",
        internal_id=42,
        name="Alice",
        role="user",
    )
    assert profile.internal_id == 42
    assert profile.id == "user_public_abc"