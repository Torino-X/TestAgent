"""Auth endpoints — login, register, logout, current user, update profile."""

import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.exceptions import AppError, InvalidCredentialsError
from app.core.response import error, success
from app.db.session import get_db
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    UpdateProfileRequest,
    UserProfile,
    UserResponse,
)
from app.services.auth_service import AuthService

logger = logging.getLogger(__name__)
router = APIRouter()


# 鉴权 cookie — 用于浏览器原生下载（绕过前端 blob URL 触发"另存为"的限制）。
# HttpOnly 阻止 JS 读取，SameSite=Lax 防 CSRF，Path=/ 让所有 API 路由可见。
# secure 仅在非 dev 启用，避免本地 http://localhost 浏览器拒绝携带。
_AUTH_COOKIE_NAME = "testagent_access_token"
_AUTH_COOKIE_MAX_AGE_SECONDS = 24 * 60 * 60  # 与 access_token_expire_minutes 对齐


def _auth_cookie_options() -> dict[str, object]:
    settings = get_settings()
    return {
        "max_age": _AUTH_COOKIE_MAX_AGE_SECONDS,
        "httponly": True,
        "samesite": "lax",
        "secure": not settings.is_development,
        "path": "/",
    }


@router.post("/login")
async def login(
    req: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_db),
):
    logger.info("登录尝试 | 账号=%s", req.account)
    try:
        service = AuthService(session)
        result = await service.login(req.account, req.password)
        # 双 channel：body 仍返回 token（兼容旧 Bearer 调用方）+ cookie 走 HttpOnly
        response.set_cookie(
            key=_AUTH_COOKIE_NAME,
            value=result.token,
            **_auth_cookie_options(),
        )
        logger.info("登录成功 | 账号=%s", req.account)
        return success(result)
    except InvalidCredentialsError as e:
        logger.warning("登录失败 | 账号=%s | 原因=凭据无效", req.account)
        return error(e.code, e.message)


@router.post("/register")
async def register(req: RegisterRequest, session: AsyncSession = Depends(get_db)):
    logger.info("注册尝试 | 邮箱=%s", req.email)
    try:
        service = AuthService(session)
        result = await service.register(req)
        logger.info("注册成功 | 邮箱=%s", req.email)
        return success(result)
    except AppError as e:
        logger.warning("注册失败 | 邮箱=%s | 原因=%s", req.email, e.message)
        return error(e.code, e.message)


@router.get("/me")
async def me(current_user: UserProfile = Depends(get_current_user)):
    """Return current user in frontend-compatible format (user_id, display_name, etc.)."""
    return success(UserResponse(
        user_id=current_user.id,
        username=current_user.username or "",
        display_name=current_user.name,
        email=current_user.email,
        role=current_user.role,
        status=current_user.status or "active",
        avatar_url=current_user.avatar_url,
    ))


@router.patch("/me")
async def update_me(
    req: UpdateProfileRequest,
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    try:
        service = AuthService(session)
        result = await service.update_profile(current_user.internal_id, req)
        return success(result)
    except AppError as e:
        return error(e.code, e.message)


@router.post("/logout")
async def logout(
    response: Response,
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Invalidate the current user's in-process config caches.

    F015 — the cache key is the integer ``users.id``; clearing it on
    logout means the next login will re-decrypt and re-cache.  We do
    not delete the DB row (the user's settings persist across
    sessions) — we just drop the in-memory decryption.  All three
    per-user configs (primary_model + knowledge_base +
    image_understanding) are cleared in one call via
    ``SettingsService.invalidate_all_user_caches``.
    """
    from app.services.settings_service import SettingsService

    logger.info("用户登出 | 用户=%s", current_user.username)
    if current_user.internal_id:
        await SettingsService(session).invalidate_all_user_caches(current_user.internal_id)
    # 必须显式带 path 才能正确清除（path 必须与 set_cookie 时一致）
    response.delete_cookie(_AUTH_COOKIE_NAME, path="/")
    return success(None, "已退出登录")


# 路由清单(Auth):
#   POST /api/v1/auth/register          注册(email + password + name)
#   POST /api/v1/auth/login             登录 → JWT + user_basic_profile
#   POST /api/v1/auth/logout            登出(可选,目前 stateless JWT)
#   GET  /api/v1/auth/me                当前 user 信息(走 deps.get_current_user)
#   PATCH /api/v1/auth/me               更新 name / email / avatar
#   POST /api/v1/auth/password          修改密码
#
# 链路:
#   register/login → AuthService.register / login →
#     crypto.hash_password / verify_password + JWT 签发
#   me / update → AuthService.get_current_user / update_profile
#
# 关键约束:
#   - 密码绝不允许进 log;hash 必须用 crypto 模块;
#   - JWT secret 必从 settings_service 读,严禁 hardcode;
#   - 注册/登录失败统一 401(凭证错误,符合 REST 语义);
#   - me 在前端每次进 App 都要调用,token 解析走 deps.py。
