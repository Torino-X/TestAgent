"""Auth Principal Cache (设计文档 §10 + 提示词 §16).

消除每个受保护 API 都跑一次 ``SELECT users`` 的压力。流程:

    Cookie / Bearer
      ↓
    JWT decode(sub)
      ↓
    AuthPrincipalCache.get_or_load(public_id)
      ├─ hit → status 校验 → UserProfile
      └─ miss
           ↓
         SingleFlight（进程内 dedupe）
           ↓
         Cache Fill Lock（跨 worker dedupe，热点 spec）
           ↓
         SELECT users
           ↓
         set 60s

缓存 DTO（**只**这 8 个字段）:

    internal_id       (int)
    public_id         (str)
    display_name      (str | None)
    username          (str | None)
    email             (str | None)
    role              (str)
    status            (str)
    avatar_url        (str | None)

明确不缓存（设计文档 §10.2 / §10.4）:
    password_hash
    last_login_at
    deleted_at 详细值（只对 deleted_at IS NULL 用户写正缓存）
    created_at / updated_at（无业务用途）

安全策略（设计文档 §10.4）:
    TTL = 60s（不用 5-30 分钟；role/status 是敏感字段）
    negative TTL = 5s
    任何 role/status/admin 写入口必须 write_through 或 invalidate
    直接改 DB 最坏 stale window = 60s
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

from app.cache.distributed_lock import CacheFillLock
from app.cache.key_builder import build_key
from app.cache.manager import CacheManager, get_cache_manager
from app.cache.metrics import cache_metrics
from app.cache.specs import CacheSpec

logger = logging.getLogger(__name__)


# ── DTO ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AuthPrincipalDTO:
    """Cache-safe projection of a ``User`` row.

    Only the 8 fields below are serialized into Redis (设计文档 §10.2).
    ``password_hash`` is NEVER held in this object nor in cache.
    """

    internal_id: int
    public_id: str
    display_name: str | None
    username: str | None
    email: str | None
    role: str
    status: str
    avatar_url: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuthPrincipalDTO":
        # Defensive: missing / extra fields are tolerated (forward-compat).
        return cls(
            internal_id=int(data["internal_id"]),
            public_id=str(data["public_id"]),
            display_name=data.get("display_name"),
            username=data.get("username"),
            email=data.get("email"),
            role=str(data["role"]),
            status=str(data["status"]),
            avatar_url=data.get("avatar_url"),
        )


# ── CacheSpec ──────────────────────────────────────────────────────


# Per 设计文档 §10.4 + §9 + 提示词 §16:
#   TTL 60s (NOT 5-30m; role/status 是敏感字段)
#   negative 5s (防穿透)
#   enable_distributed_fill_lock=True (热点 spec：每请求都查)
#   max_value_bytes 默认 256 KiB
AUTH_PRINCIPAL_SPEC = CacheSpec(
    domain="auth",
    ttl_seconds=60,
    negative_ttl_seconds=5,
    jitter_ratio=0.10,
    enable_singleflight=True,
    enable_distributed_fill_lock=True,
)


def auth_principal_key(public_id: str) -> str:
    """Build the canonical Redis key for an auth principal entry."""
    return build_key("auth", "principal", public_id)


# ── Cache service ──────────────────────────────────────────────────


class AuthPrincipalCache:
    """Cache-aside accessor for ``User`` principal DTOs.

    Typical wiring from ``backend/app/api/deps.py``::

        from app.cache.domains.auth_cache import AuthPrincipalCache

        cache = AuthPrincipalCache(get_cache_manager(), fill_lock)
        principal = await cache.get_or_load(
            public_id=public_id,
            loader=lambda: _load_user_from_db(public_id, user_repo),
        )
        if principal is None:
            raise _unauthorized(...)           # not found / soft-deleted
        if principal.status != "active":
            raise _unauthorized(...disabled)   # disabled
        return _to_user_profile(principal)
    """

    def __init__(
        self,
        manager: CacheManager | None = None,
        *,
        fill_lock: CacheFillLock | None = None,
    ) -> None:
        self._mgr = manager if manager is not None else get_cache_manager()
        # Fill lock is only constructed when (a) the spec enables it
        # and (b) we actually have a redis client.  Lazy.
        self._fill_lock = fill_lock

    @property
    def spec(self) -> CacheSpec:
        return AUTH_PRINCIPAL_SPEC

    def _key(self, public_id: str) -> str:
        return auth_principal_key(public_id)

    def _fill_lock_runtime(self) -> CacheFillLock | None:
        """Lazy-construct a fill lock against the manager's redis client.

        Only constructed when:
          - spec.enable_distributed_fill_lock=True (always for auth), AND
          - CacheManager.is_enabled() returns True (client attached).
        """
        if self._fill_lock is not None:
            return self._fill_lock
        if not self._mgr.is_enabled() or not self.spec.enable_distributed_fill_lock:
            return None
        try:
            backend = self._mgr._backend  # type: ignore[attr-defined]
            client = backend.client
        except RuntimeError:
            return None
        from app.core.config import get_settings

        ttl_ms = int(get_settings().cache_lock_ttl_ms)
        self._fill_lock = CacheFillLock(client, ttl_ms=ttl_ms)
        return self._fill_lock

    # ── Read path ───────────────────────────────────────────────────

    async def get_or_load(
        self,
        public_id: str,
        loader,
    ) -> AuthPrincipalDTO | None:
        """Cache-aside: hit returns DTO, miss invokes ``loader()``.

        ``loader()`` must return ``AuthPrincipalDTO`` for active users or
        ``None`` for not-found / soft-deleted users (the latter triggers
        a negative envelope).

        The cached payload is the DTO's dict form (designed for Redis
        JSON storage).  On hit, ``cached_adapter`` reconstructs the DTO
        for the caller so the contract is uniform.

        Negative caches are short-lived (5s) to limit the blast radius
        of a transient inconsistency (e.g. user deleted between JWT
        issue and request).
        """
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec.domain):
            # Bypass path: just call the loader directly.
            return await loader()

        # Miss adapter: convert loader return value to dict for cache.
        async def _adapt_loader():
            result = await loader()
            if result is None:
                return None
            if isinstance(result, AuthPrincipalDTO):
                return result.to_dict()
            if isinstance(result, dict):
                return result
            raise TypeError(
                f"AuthPrincipalCache loader must return AuthPrincipalDTO "
                f"or None, got {type(result).__name__}"
            )

        # Hit adapter: convert cached dict back to DTO for callers.
        # Negative hits map to None (the auth-cache contract: "no such
        # user" = None, regardless of whether the cache held a negative
        # envelope or a positive entry that deserialised to None).
        async def _adapt_cached(raw):
            from app.cache.manager import _CacheMiss as _CM

            if isinstance(raw, _CM):
                return None
            if isinstance(raw, dict):
                return AuthPrincipalDTO.from_dict(raw)
            return raw

        return await self._mgr.get_or_load(
            self.spec,
            self._key(public_id),
            _adapt_loader,
            fill_lock=self._fill_lock_runtime(),
            cached_adapter=_adapt_cached,
        )

    # ── Write-through (after DB commit) ────────────────────────────

    async def write_through(
        self,
        public_id: str,
        dto: AuthPrincipalDTO | None,
    ) -> bool:
        """Cache write-through after a successful DB commit.

        Pass ``dto=None`` to delete the entry (e.g. admin disable).
        Returns True if the cache was updated, False if bypassed.

        MUST be called AFTER the DB commit (设计文档 §17.1 + 提示词 §4
        第 14 条) — otherwise a rollback would leave the cache
        permanently stale.
        """
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec.domain):
            cache_metrics.record(
                domain=self.spec.domain, operation="set", result="bypass",
            )
            return False
        if dto is None:
            return await self._mgr.delete(self.spec, self._key(public_id))
        return await self._mgr.set(self.spec, self._key(public_id), dto.to_dict())

    # ── Explicit invalidation (rare) ───────────────────────────────

    async def invalidate(self, public_id: str) -> bool:
        """Remove the cached entry.  Use when ``write_through`` is not
        appropriate (e.g. user soft-deleted and we don't have a DTO)."""
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec.domain):
            return False
        return await self._mgr.delete(self.spec, self._key(public_id))


# ── Helpers ────────────────────────────────────────────────────────

# (No module-level loader wrapper needed; the adapter is inlined in
# AuthPrincipalCache.get_or_load so that CacheManager.get_or_load sees
# an async function, not a pre-invoked coroutine.)

# ── Module-level singleton ─────────────────────────────────────────

_cache: AuthPrincipalCache | None = None


def get_auth_principal_cache() -> AuthPrincipalCache:
    """Return the lifespan-installed singleton (lazy default).

    Production code should call this; tests can override via
    ``set_auth_principal_cache(...)``.
    """
    global _cache
    if _cache is None:
        _cache = AuthPrincipalCache()
    return _cache


def set_auth_principal_cache(cache: AuthPrincipalCache | None) -> None:
    """Install/reset the singleton (mainly for tests)."""
    global _cache
    _cache = cache


__all__ = [
    "AUTH_PRINCIPAL_SPEC",
    "AuthPrincipalCache",
    "AuthPrincipalDTO",
    "auth_principal_key",
    "get_auth_principal_cache",
    "set_auth_principal_cache",
]