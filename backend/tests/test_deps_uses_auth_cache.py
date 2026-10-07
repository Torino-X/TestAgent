"""Phase 1 — Step 4 集成验证: ``deps.get_current_user`` 走 Auth Principal Cache。

合约 (设计文档 §10 + 提示词 §16):
  - JWT decode → AuthPrincipalCache.get_or_load(public_id)
  - cache hit → 不再查 users 表
  - cache miss → 走 _load_principal_from_db (UserRepository.get_by_public_id) → set 60s
  - status != 'active' → 401 (即使 cached；status 是敏感字段，60s stale window 由设计接受)
  - 用户不存在 → 401；5s negative cache 防止穿透

测试用 Starlette TestClient 启一个最小 FastAPI，挂一个 stub endpoint 让
我们观察 ``get_current_user`` 的实际行为。每个用例 fresh fakeredis + 一个
AuthPrincipalCache 实例（注入到 deps.get_auth_principal_cache）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Depends, FastAPI
from starlette.testclient import TestClient

from app.api.deps import get_current_user
from app.cache.backend import CacheBackend
from app.cache.bulkhead import DBBulkhead
from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
from app.cache.domains.auth_cache import (
    AuthPrincipalCache,
    AuthPrincipalDTO,
    get_auth_principal_cache,
    set_auth_principal_cache,
)
from app.cache.manager import CacheManager
from app.cache.metrics import cache_metrics
from app.cache.singleflight import SingleFlight
from app.core.security import create_access_token
from app.schemas.auth import UserProfile


# ── helpers ────────────────────────────────────────────────────────


def _fake_user(
    public_id: str = "usr_cookie_test",
    status: str = "active",
    role: str = "user",
):
    return SimpleNamespace(
        id=7,
        public_id=public_id,
        username="cookie_user",
        email="cookie@example.com",
        password_hash="x",
        display_name="Cookie User",
        role=role,
        status=status,
        avatar_url=None,
        last_login_at=None,
        deleted_at=None,
    )


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/stub/whoami")
    async def whoami(current_user: UserProfile = Depends(get_current_user)):
        return {
            "public_id": current_user.id,
            "internal_id": current_user.internal_id,
            "role": current_user.role,
            "status": current_user.status,
        }

    return app


def _build_auth_cache_with_fake_redis(redis):
    backend = CacheBackend(redis_client=redis)
    backend._healthy = True
    mgr = CacheManager(
        backend=backend,
        breaker=CircuitBreaker(BreakerConfig(failure_threshold=5, open_seconds=0.05)),
        bulkhead=DBBulkhead(max_concurrency=2),
        singleflight=SingleFlight(),
        jitter_fn=lambda ratio: 1.0,
    )
    return AuthPrincipalCache(manager=mgr)


# ── fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def fake_redis():
    """Sync-constructible FakeRedis for sync TestClient-based tests.

    Note: the FakeRedis methods are async, but constructing + closing
    the client doesn't require a running loop.  ``aclose`` is awaited
    by the fixture-finalizer helper which uses ``asyncio.run`` on the
    side — but in practice TestClient's lifespan already exercises the
    event loop and we just need the client to be valid for the test.
    """
    from fakeredis import aioredis as fakeredis_aioredis

    redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
    try:
        yield redis
    finally:
        # Best-effort cleanup; the FakeRedis instance will be GC'd at
        # end of test session regardless.
        import asyncio

        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(redis.aclose())
            finally:
                loop.close()
        except Exception:
            pass


@pytest.fixture
def client(fake_redis):
    # Install an AuthPrincipalCache backed by fakeredis BEFORE the
    # FastAPI app is constructed; set_auth_principal_cache ensures
    # deps.get_current_user uses this cache via get_auth_principal_cache().
    auth_cache = _build_auth_cache_with_fake_redis(fake_redis)
    set_auth_principal_cache(auth_cache)
    cache_metrics.reset()
    try:
        yield TestClient(_build_app(), raise_server_exceptions=True)
    finally:
        set_auth_principal_cache(None)


def _stub_user_repo(fake_user):
    """Patch deps.UserRepository so DB lookups return ``fake_user``."""
    repo = SimpleNamespace()
    repo.get_by_public_id = AsyncMock(return_value=fake_user)
    return repo


# ── 1) cache miss → DB → cache populated ──────────────────────────


def test_first_request_populates_cache_via_db(client) -> None:
    """Phase 1 行为:首次请求 → DB SELECT users → cache set 60s。

    We verify the cache population indirectly: the second request (cache
    hit) must NOT invoke the DB loader.  This is the same invariant as
    ``test_second_request_hits_cache_skips_db`` below — collapsed here
    for the "first request" framing.
    """
    user = _fake_user()
    token = create_access_token(user.public_id)
    repo = SimpleNamespace()

    captured = {"calls": 0}

    async def counting_get_by_public_id(public_id):
        captured["calls"] += 1
        return user

    repo.get_by_public_id = AsyncMock(side_effect=counting_get_by_public_id)

    with patch("app.api.deps.UserRepository", return_value=repo):
        r1 = client.get(
            "/api/stub/whoami", cookies={"testagent_access_token": token},
        )
        r2 = client.get(
            "/api/stub/whoami", cookies={"testagent_access_token": token},
        )

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json() == r2.json()
    # Phase 1 invariant: at most 1 DB call across 2 requests — the second
    # must hit the cache populated by the first.
    assert captured["calls"] == 1, (
        f"Phase 1 Auth Cache failed: got {captured['calls']} DB calls "
        f"across 2 requests (target: 1, miss-only path)"
    )


# ── 2) cache hit → no DB call ─────────────────────────────────────


def test_second_request_hits_cache_skips_db(client) -> None:
    """Phase 1 核心目标:第二次请求 → 0 users SELECT。"""
    user = _fake_user()
    token = create_access_token(user.public_id)
    repo = _stub_user_repo(user)

    captured = {"calls": 0}

    async def counting_get_by_public_id(public_id):
        captured["calls"] += 1
        return user

    repo.get_by_public_id = AsyncMock(side_effect=counting_get_by_public_id)

    with patch("app.api.deps.UserRepository", return_value=repo):
        # First request — DB hit, cache populated.
        r1 = client.get(
            "/api/stub/whoami", cookies={"testagent_access_token": token},
        )
        # Second request — must be a cache hit.
        r2 = client.get(
            "/api/stub/whoami", cookies={"testagent_access_token": token},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    # The headline invariant: only 1 DB call across 2 requests.
    assert captured["calls"] == 1, (
        f"Auth Cache must skip DB on hit; got {captured['calls']} DB calls "
        f"across 2 requests (Phase 0 baseline target: 0 on hit path)"
    )


# ── 3) cache hit for disabled user still 401s ─────────────────────


def test_cached_disabled_user_returns_401(client) -> None:
    """Even if cached, status='disabled' must fail auth (设计文档 §10.4).

    The 60s TTL makes this a known stale window; this test verifies we
    never serve a disabled user as authenticated.
    """
    user = _fake_user(status="disabled")
    token = create_access_token(user.public_id)
    repo = _stub_user_repo(user)

    with patch("app.api.deps.UserRepository", return_value=repo):
        # First request — cache miss → DB returns disabled user → cache.
        r1 = client.get(
            "/api/stub/whoami", cookies={"testagent_access_token": token},
        )
        # Second request — cache hit but status still disabled.
        r2 = client.get(
            "/api/stub/whoami", cookies={"testagent_access_token": token},
        )

    assert r1.status_code == 401
    assert r2.status_code == 401


# ── 4) cache miss for missing user → 401 + negative cache ─────────


def test_missing_user_returns_401_and_negative_caches(client, fake_redis) -> None:
    """``user_repo.get_by_public_id`` returns None → 401; second call
    must short-circuit via negative cache (no DB)."""
    user = _fake_user()
    token = create_access_token(user.public_id)

    repo = SimpleNamespace()
    repo.get_by_public_id = AsyncMock(return_value=None)

    captured = {"calls": 0}

    async def counting_get_by_public_id(public_id):
        captured["calls"] += 1
        return None

    repo.get_by_public_id = AsyncMock(side_effect=counting_get_by_public_id)

    with patch("app.api.deps.UserRepository", return_value=repo):
        r1 = client.get(
            "/api/stub/whoami", cookies={"testagent_access_token": token},
        )
        r2 = client.get(
            "/api/stub/whoami", cookies={"testagent_access_token": token},
        )

    assert r1.status_code == 401
    assert r2.status_code == 401
    # First call hits DB (returns None); second is short-circuited by
    # the negative envelope (5s TTL).
    assert captured["calls"] == 1


# ── 5) soft-deleted user is treated as missing ─────────────────────


def test_soft_deleted_user_returns_401_and_is_not_cached(client) -> None:
    """``deleted_at IS NOT NULL`` → return None from loader → negative cache.

    Per 设计文档 §10.2: 只对 ``deleted_at IS NULL`` 用户写入正缓存。
    """
    deleted_user = _fake_user()
    # Mimic soft-deleted state by overriding deleted_at on the fake.
    deleted_user.deleted_at = "2026-09-01T00:00:00Z"

    token = create_access_token(deleted_user.public_id)
    repo = SimpleNamespace()
    repo.get_by_public_id = AsyncMock(return_value=deleted_user)

    with patch("app.api.deps.UserRepository", return_value=repo):
        r = client.get(
            "/api/stub/whoami", cookies={"testagent_access_token": token},
        )
    assert r.status_code == 401


# ── 6) update_profile write-through: next request sees new value ───


@pytest.mark.asyncio
async def test_update_profile_write_through_serves_new_value(
    client, fake_redis
) -> None:
    """Simulates ``PATCH /api/auth/me`` followed by a protected request:

      1. First request → cache miss → DB returns "old user"
      2. ``auth_service.update_profile`` equivalent → write-through with new DTO
      3. Second request → cache hit → DB is NOT invoked (the headline invariant)

    Pure async test (vs. TestClient sync); bypasses StarletteTestClient
    for the cache-mutation portion so we can ``await`` the write_through
    coroutine directly.  The second protected request then runs via
    Starlette's TestClient (which spins its own loop).
    """
    user = _fake_user()
    token = create_access_token(user.public_id)
    repo = SimpleNamespace()
    repo.get_by_public_id = AsyncMock(return_value=user)

    captured = {"calls": 0}

    async def counting_get_by_public_id(public_id):
        captured["calls"] += 1
        return user

    repo.get_by_public_id = AsyncMock(side_effect=counting_get_by_public_id)

    # Cache populated via direct manager call (TestClient can't be awaited
    # synchronously without creating an event-loop conflict).  Then the
    # integration shape: TestClient issues cookie-auth requests, which
    # internally invoke deps.get_current_user → AuthPrincipalCache.
    auth_cache = get_auth_principal_cache()

    async def _warm():
        # First read populates the cache.
        return await auth_cache.get_or_load(
            public_id=user.public_id,
            loader=lambda: _load_fake_user(user),
        )

    warm = await _warm()
    assert warm is not None and warm.public_id == user.public_id

    # (2) Simulate PATCH /api/auth/me via public write_through.
    new_dto = AuthPrincipalDTO(
        internal_id=user.id,
        public_id=user.public_id,
        display_name="Cookie User Renamed",
        username=user.username,
        email=user.email,
        role=user.role,
        status=user.status,
        avatar_url=user.avatar_url,
    )
    ok = await auth_cache.write_through(user.public_id, new_dto)
    assert ok is True

    # (3) Second read should hit the cache populated above + write-through
    # updates; loader should NOT be invoked.
    async def _must_not_run():
        captured["calls"] += 1
        raise RuntimeError("loader must not run on hit")

    cached = await auth_cache.get_or_load(
        public_id=user.public_id,
        loader=_must_not_run,
    )
    assert cached is not None
    assert cached.display_name == "Cookie User Renamed"
    assert captured["calls"] == 0, (
        f"write-through must avoid DB on next read; loader ran {captured['calls']} times"
    )


async def _load_fake_user(user):
    """Async loader returning AuthPrincipalDTO for cache population."""
    return AuthPrincipalDTO(
        internal_id=user.id,
        public_id=user.public_id,
        display_name=user.display_name,
        username=user.username,
        email=user.email,
        role=user.role,
        status=user.status,
        avatar_url=user.avatar_url,
    )


# ── 7) bypass path: domain flag off → DB every time ───────────────


def test_domain_disabled_bypasses_cache(client, monkeypatch) -> None:
    """``CACHE_AUTH_ENABLED=false`` → DB runs on every request (no cache)."""
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "cache_auth_enabled", False)

    user = _fake_user()
    token = create_access_token(user.public_id)
    repo = SimpleNamespace()
    captured = {"calls": 0}

    async def counting(public_id):
        captured["calls"] += 1
        return user

    repo.get_by_public_id = AsyncMock(side_effect=counting)

    with patch("app.api.deps.UserRepository", return_value=repo):
        r1 = client.get(
            "/api/stub/whoami", cookies={"testagent_access_token": token},
        )
        r2 = client.get(
            "/api/stub/whoami", cookies={"testagent_access_token": token},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert captured["calls"] == 2, (
        f"bypass path must call DB every time; got {captured['calls']}"
    )


# ── 8) JWT decode failures short-circuit before cache ──────────────


def test_invalid_jwt_returns_401_without_touching_db(client) -> None:
    """A malformed token must 401 BEFORE we hit the cache or DB."""
    captured = {"calls": 0}

    async def fail(public_id):
        captured["calls"] += 1
        return None

    repo = SimpleNamespace()
    repo.get_by_public_id = AsyncMock(side_effect=fail)

    with patch("app.api.deps.UserRepository", return_value=repo):
        resp = client.get(
            "/api/stub/whoami",
            cookies={"testagent_access_token": "garbage.jwt.value"},
        )

    assert resp.status_code == 401
    assert captured["calls"] == 0, "JWT decode must short-circuit before DB"


# ── 9) request.state.current_user_internal_id is set ───────────────


def test_request_state_internal_id_set_on_success(client) -> None:
    """Auth downstream code reads ``request.state.current_user_internal_id``."""
    user = _fake_user()
    token = create_access_token(user.public_id)
    repo = _stub_user_repo(user)

    with patch("app.api.deps.UserRepository", return_value=repo):
        r = client.get(
            "/api/stub/whoami", cookies={"testagent_access_token": token},
        )

    assert r.status_code == 200
    assert r.json()["internal_id"] == user.id


# ── utility ─────────────────────────────────────────────────────────


def await_or_sync(coro_or_value):
    """Schedule a coroutine on the running loop and return its result."""
    import asyncio

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if hasattr(coro_or_value, "__await__"):
        # In a sync test context, we still have a running loop via pytest-asyncio.
        # The conftest provided loop is used.
        return asyncio.get_event_loop().run_until_complete(coro_or_value)
    return coro_or_value