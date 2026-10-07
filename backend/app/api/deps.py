"""API dependencies — shared FastAPI Depends() callables."""

from __future__ import annotations

import logging

from fastapi import Depends, Header, Request, Security
from fastapi.exceptions import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer
from jose import JWTError

from app.core.security import decode_access_token
from app.db.session import AsyncSessionLocal
from app.repositories.user_repository import UserRepository  # noqa: E402 — module-level for test patching
from app.schemas.auth import UserProfile

# Phase 1 — Redis Cache project (Step 4): Auth Principal 走缓存
# 避免每个受保护 API 都跑一次 SELECT users（设计文档 §10）。
# Login / register 不走这里（拿到 token 后才走 deps.get_current_user）。
from app.cache.domains.auth_cache import (
    AuthPrincipalDTO,
    get_auth_principal_cache,
)

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)
# The HTTP Bearer scheme makes protected endpoints usable from Swagger UI's
# built-in Authorize control.  Keep the hidden raw-header fallback below for
# existing scripts and direct dependency tests.
swagger_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(code: int, message: str) -> HTTPException:
    """Build a 401 HTTPException with the project's unified response envelope."""
    from app.core.response import error

    return HTTPException(
        status_code=401,
        detail=error(code, message),
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _load_principal_from_db(
    public_id: str,
) -> AuthPrincipalDTO | None:
    """DB loader passed to ``AuthPrincipalCache.get_or_load``.

    Opens its own short-lived session (no greenlet conflict with the
    request-scoped session).  Returns:
      * ``AuthPrincipalDTO`` for active (deleted_at IS NULL) users
      * ``None`` for not-found / soft-deleted users

    Per 设计文档 §10.2: deleted users MUST NOT be cached as a positive
    payload.  The cache layer short-circuits subsequent requests for
    the same public_id via the negative envelope (5s TTL).
    """
    async with AsyncSessionLocal() as session:
        user_repo = UserRepository(session)
        user = await user_repo.get_by_public_id(public_id)
        if user is None:
            return None
        # Use getattr for soft-delete check so SimpleNamespace-based
        # test fakes (which may omit ``deleted_at``) don't crash.
        if getattr(user, "deleted_at", None) is not None:
            return None
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


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(swagger_bearer_scheme),
    authorization: str = Header(default="", include_in_schema=False),
) -> UserProfile:
    """Resolve the current user from cookie or Bearer token.

    Token resolution order (Plan 1 — Artifact Download Cookie Auth):
      1. HttpOnly cookie ``testagent_access_token`` (set by /api/auth/login)
         — used by browser-driven flows (notably artifact downloads
         via hidden iframe, where custom headers cannot be attached).
      2. ``Authorization: Bearer <token>`` header — kept for non-browser
         callers (curl, scripts, E2E) and as a fallback if cookie auth
         is somehow unavailable.

    Phase 1 (Redis Cache project, Step 4): JWT decode → cache hit
    avoids the users SELECT.  Cache miss falls through to
    ``_load_principal_from_db``; result is cached with 60s TTL
    (设计文档 §10.4 — role/status 是敏感字段，TTL 不超过 60s).
    """
    token = ""
    # 1) Cookie 优先（HttpOnly，浏览器自动带；下载场景靠这一路）
    cookie_token = request.cookies.get("testagent_access_token")
    if cookie_token:
        token = cookie_token
    # 2) 兜底走 Bearer header（兼容旧调用方/工具/script）
    elif isinstance(credentials, HTTPAuthorizationCredentials):
        token = credentials.credentials
    elif authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]

    if not token:
        logger.debug("鉴权失败 | 原因=未提供Token | path=%s", getattr(request, "url", "?"))
        raise _unauthorized(40100, "未登录或登录状态已失效")

    # Decode JWT to get the user's public_id
    try:
        payload = decode_access_token(token)
    except JWTError:
        logger.debug("鉴权失败 | 原因=JWT解码失败 | path=%s", getattr(request, "url", "?"))
        raise _unauthorized(40101, "Token 无效或已过期")

    public_id = payload.get("sub")
    if not public_id:
        logger.debug("鉴权失败 | 原因=JWT无sub字段 | path=%s", getattr(request, "url", "?"))
        raise _unauthorized(40101, "Token 无效或已过期")

    # Phase 1 (Step 4): 缓存查找 → miss 时 DB → set 60s
    auth_cache = get_auth_principal_cache()
    principal = await auth_cache.get_or_load(
        public_id=public_id,
        loader=lambda: _load_principal_from_db(public_id),
    )

    if principal is None:
        logger.debug("鉴权失败 | 原因=用户不存在或已删除 | public_id=%s", public_id)
        raise _unauthorized(40100, "未登录或登录状态已失效")

    if principal.status != "active":
        logger.warning("鉴权失败 | 原因=账号被禁用 | public_id=%s", public_id)
        raise _unauthorized(40100, "账号已被禁用")

    # Expose internal_id for downstream handlers that prefer to read it
    # from request.state rather than the UserProfile.
    request.state.current_user_internal_id = principal.internal_id

    logger.debug(
        "鉴权通过 | public_id=%s | role=%s | internal_id=%s",
        principal.public_id, principal.role, principal.internal_id,
    )

    return UserProfile(
        id=principal.public_id,
        internal_id=principal.internal_id,
        name=principal.display_name or principal.username or "",
        role=principal.role,
        username=principal.username,
        email=principal.email,
        status=principal.status,
        avatar_url=principal.avatar_url,
    )


def _forbidden(code: int, message: str) -> HTTPException:
    """Build a 403 HTTPException with the project's unified response envelope."""
    from app.core.response import error

    return HTTPException(
        status_code=403,
        detail=error(code, message),
    )


async def require_role(
    role: str,
    current: UserProfile = Depends(get_current_user),
) -> UserProfile:
    """已认证且角色为给定值（当前 role 值仅 user/admin）。"""
    if current.role != role:
        raise _forbidden(40301, f"需要角色 {role}")
    return current


async def require_admin(
    current: UserProfile = Depends(get_current_user),
) -> UserProfile:
    """已认证且角色为 admin。"""
    if current.role != "admin":
        raise _forbidden(40301, "需要管理员权限")
    return current


async def require_workspace(
    workspace_key: str,
    current: UserProfile = Depends(get_current_user),
) -> str:
    """Owner-scoped workspace 校验。

    workspace_key 是 user-scoped 逻辑键（无 Workspace Membership 表）。
    空 workspace_key 视为不属于任何 workspace（返回 None，调用方按 owner 过滤）。
    """
    if workspace_key == "":
        return ""
    return workspace_key


# ════════════════════════════════════════════════════════════════════════════════
# API 层 FastAPI Depends() 共享依赖项:
#
#   路由文件里使用 ``Depends(get_current_user)`` / ``Depends(get_db_session)``
#   等,所有依赖项函数在这里集中实现,便于:
#     - 全局可观察(看这里就知道每条路由依赖什么);
#     - 注入 mock 时一处生效(测试时 override 这里的函数);
#     - 鉴权逻辑统一(把 AuthService 解码逻辑封装到 get_current_user)。
#
# 常见依赖函数:
#   - get_db_session: yield 一个 AsyncSession(per-request lifecycle);
#   - get_current_user: 解析 Authorization header → user 行 + user_internal_id;
#   - get_settings_service: 读 model_configs / knowledge_configs 注入点;
#   - get_event_sink: 给 SSE handler 注入 SSE 推送器。
#
# 关键约束(供开发者速查):
#   - yield 依赖只能用于 AsyncSession / generator 风格资源,不要写成 async def;
#   - Depends 失败 → 自动 raise HTTPException(401 / 403),handler 不用手动 try;
#   - 测试覆盖:用 pytest fixture override 这些函数,不直接访问数据库。
#   - Phase 1 (Step 4): get_current_user 内部走 AuthPrincipalCache,
#     用户不存在/软删除时返回 None (负缓存 5s)；role/status 走缓存后
#     仍然必须在调用方校验 status="active"(防止 stale 60s 窗口内被
#     禁用的账号继续访问)。
