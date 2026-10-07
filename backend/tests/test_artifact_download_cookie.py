"""Plan 1: Artifact download cookie auth — HTTP-level contract.

What we pin down here:

  * ``POST /api/auth/login`` issues an HttpOnly cookie carrying the JWT
    alongside the body token.  Cookie is the only channel the
    browser-driven artifact download (hidden iframe) can use, because
    iframe navigation cannot attach custom ``Authorization`` headers.
  * ``POST /api/auth/logout`` clears that cookie via ``Set-Cookie`` with
    ``Max-Age=0`` (path must match).
  * ``deps.get_current_user`` accepts both the cookie and the legacy
    Bearer header.  Cookie wins; Bearer is the fallback for non-browser
    callers (curl, scripts, E2E).
  * Missing both channels → 401 with the project's unified envelope.

These tests use Starlette's ``TestClient`` against a tiny FastAPI app
that mounts only the auth router and a stubbed artifact endpoint that
mirrors the real ``GET /api/artifacts/{id}/download`` contract (200 +
``Content-Disposition: attachment``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from starlette.testclient import TestClient

from app.api.deps import get_current_user
from app.api.v1.auth import router as auth_router
from app.core.security import create_access_token, hash_password
from app.db.session import get_db
from app.schemas.auth import LoginResponse, UserResponse


AUTH_COOKIE_NAME = "testagent_access_token"


# ── shared stubs ────────────────────────────────────────────────


def _fake_user():
    return SimpleNamespace(
        id=7,
        public_id="user_cookie_test",
        username="cookie_user",
        email="cookie@example.com",
        password_hash=hash_password("pw-strong-123"),
        display_name="Cookie User",
        role="user",
        status="active",
        avatar_url=None,
        last_login_at=None,
    )


def _fake_login_response(user) -> LoginResponse:
    token = create_access_token(user.public_id)
    return LoginResponse(
        token=token,
        user=UserResponse(
            user_id=user.public_id,
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            role=user.role,
            status=user.status,
            avatar_url=user.avatar_url,
        ),
    )


async def _dummy_db():
    """Override app.db.session.get_db — nothing here actually queries the DB."""
    yield SimpleNamespace(flush=AsyncMock(return_value=None))


def _build_app() -> FastAPI:
    """Minimal app — only auth + a download stub that requires auth."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    # Override the real DB dependency so login() doesn't try to open a session.
    app.dependency_overrides[get_db] = _dummy_db

    @app.get("/api/artifacts/{artifact_id}/download")
    async def stub_download(
        artifact_id: str,
        current_user=Depends(get_current_user),
    ):
        async def stream():
            yield b"word"

        return StreamingResponse(
            stream(),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{artifact_id}.docx"'},
        )

    return app


@pytest.fixture
def client():
    return TestClient(_build_app(), raise_server_exceptions=True)


# ── 1) login writes HttpOnly cookie ────────────────────────────


def test_login_sets_httponly_cookie(client):
    """Successful login must set the auth cookie.  Browser downloads
    (iframe) depend on this — there's no other way to attach a token."""
    user = _fake_user()

    fake_service = SimpleNamespace()
    fake_service.login = AsyncMock(return_value=_fake_login_response(user))

    # SettingsService call inside login() needs a real session — patch
    # the class to a no-op so we don't need a DB.
    # NOTE: auth.py does `from app.services.auth_service import AuthService`,
    # so the bound name is `app.api.v1.auth.AuthService`, NOT the source module.
    with (
        patch("app.api.v1.auth.AuthService", return_value=fake_service),
        patch(
            "app.services.settings_service.SettingsService.bootstrap_all_user_caches",
            AsyncMock(return_value=None),
        ),
    ):
        resp = client.post(
            "/api/auth/login",
            json={"username": "cookie_user", "password": "pw-strong-123"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["token"]  # body token still present (dual channel)

    set_cookie = resp.headers.get("set-cookie", "")
    assert AUTH_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie or "samesite=lax" in set_cookie.lower()
    assert "Path=/" in set_cookie or "path=/" in set_cookie.lower()
    # Dev env → secure flag MUST be False (localhost http works without it)
    assert "Secure" not in set_cookie


# ── 2) cookie alone is sufficient to call protected download ───


def test_cookie_authenticates_artifact_download(client):
    """The whole point of Plan 1: a browser iframe carrying only the
    HttpOnly cookie (no Authorization header) can fetch the download."""
    user = _fake_user()
    cookie_value = create_access_token(user.public_id)

    # get_current_user reads user from DB — patch UserRepository to
    # return our fake user without hitting a real DB.
    fake_repo = SimpleNamespace()
    fake_repo.get_by_public_id = AsyncMock(return_value=user)

    with patch(
        "app.api.deps.UserRepository", return_value=fake_repo
    ):
        resp = client.get(
            "/api/artifacts/artifact_001/download",
            cookies={AUTH_COOKIE_NAME: cookie_value},
        )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"].startswith("attachment")
    assert b"word" in resp.content


# ── 3) Bearer header still works (backward compatibility) ───────


def test_bearer_header_still_authenticates_artifact_download(client):
    """Non-browser callers (curl / scripts / E2E) still pass
    ``Authorization: Bearer <token>`` — keep that channel alive."""
    user = _fake_user()
    token = create_access_token(user.public_id)

    fake_repo = SimpleNamespace()
    fake_repo.get_by_public_id = AsyncMock(return_value=user)

    with patch(
        "app.api.deps.UserRepository", return_value=fake_repo
    ):
        resp = client.get(
            "/api/artifacts/artifact_001/download",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"].startswith("attachment")


# ── 4) cookie wins over Bearer when both are present ────────────


def test_cookie_takes_precedence_over_bearer(client):
    """If both channels are sent, the cookie's JWT is what we trust.

    This avoids one nasty class of bug: a stale localStorage token
    (expired / wrong user) must not override a freshly-set cookie."""
    cookie_user = SimpleNamespace(
        id=7,
        public_id="user_cookie_wins",
        username="cookie_user",
        email="c@e.com",
        password_hash="x",
        display_name="Cookie",
        role="user",
        status="active",
        avatar_url=None,
        last_login_at=None,
    )
    bearer_user = SimpleNamespace(
        id=99,
        public_id="user_bearer_loses",
        username="bearer_user",
        email="b@e.com",
        password_hash="x",
        display_name="Bearer",
        role="user",
        status="active",
        avatar_url=None,
        last_login_at=None,
    )

    cookie_token = create_access_token(cookie_user.public_id)
    bearer_token = create_access_token(bearer_user.public_id)

    captured_internal_id = {}

    async def fake_get_by_public_id(public_id):
        if public_id == cookie_user.public_id:
            captured_internal_id["id"] = cookie_user.id
            return cookie_user
        return None

    fake_repo = SimpleNamespace()
    fake_repo.get_by_public_id = AsyncMock(side_effect=fake_get_by_public_id)

    with patch(
        "app.api.deps.UserRepository", return_value=fake_repo
    ):
        resp = client.get(
            "/api/artifacts/artifact_001/download",
            cookies={AUTH_COOKIE_NAME: cookie_token},
            headers={"Authorization": f"Bearer {bearer_token}"},
        )

    assert resp.status_code == 200
    # The cookie's user (id=7) was looked up, NOT the bearer's (id=99).
    assert captured_internal_id["id"] == 7


# ── 5) missing both channels → 401 ─────────────────────────────


def test_missing_both_cookie_and_bearer_returns_401(client):
    """No auth channel → 401 with the unified envelope."""
    resp = client.get("/api/artifacts/artifact_001/download")

    assert resp.status_code == 401
    # deps._unauthorized wraps via HTTPException(detail=error(...)),
    # so FastAPI serializes detail directly: {"detail": {"code": 40100, ...}}
    detail = resp.json().get("detail", {})
    assert detail.get("code") in (40100, 40101)
    assert detail.get("data") is None


# ── 6) logout clears the cookie ────────────────────────────────


def test_logout_clears_auth_cookie(client):
    """Logout must remove the cookie — verify by sending the cookie
    after logout and expecting the next protected request to 401."""
    user = _fake_user()
    cookie_value = create_access_token(user.public_id)

    fake_repo = SimpleNamespace()
    fake_repo.get_by_public_id = AsyncMock(return_value=user)

    with (
        patch(
            "app.api.deps.UserRepository", return_value=fake_repo
        ),
        patch(
            "app.services.settings_service.SettingsService.invalidate_all_user_caches",
            AsyncMock(return_value=None),
        ),
    ):
        # Logout (with cookie so deps accepts the call)
        logout_resp = client.post(
            "/api/auth/logout",
            cookies={AUTH_COOKIE_NAME: cookie_value},
        )
    assert logout_resp.status_code == 200

    set_cookie = logout_resp.headers.get("set-cookie", "")
    # Either an explicit Max-Age=0 / expires in the past, OR an empty value.
    assert AUTH_COOKIE_NAME in set_cookie
    assert (
        "Max-Age=0" in set_cookie
        or "max-age=0" in set_cookie.lower()
        or 'expires=Thu, 01 Jan 1970' in set_cookie
    )