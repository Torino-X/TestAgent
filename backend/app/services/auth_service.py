"""Auth service — login, register, token management, profile update."""

from __future__ import annotations

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, InvalidCredentialsError, ValidationError
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import (
    AuthTokenResponse,
    LoginResponse,
    RegisterRequest,
    UpdateProfileRequest,
    UserProfile,
    UserResponse,
)
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)


class AuthService:
    """Authentication service — real database lookups."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._user_repo = UserRepository(session)

    async def login(self, account: str, password: str) -> LoginResponse:
        """Authenticate with username/email + password against the users table."""
        if not account or not password:
            raise InvalidCredentialsError()

        account = account.strip()
        user = await self._user_repo.get_by_username(account)
        if not user:
            user = await self._user_repo.get_by_email(account)
        if not user:
            raise InvalidCredentialsError()

        if not verify_password(password, user.password_hash):
            raise InvalidCredentialsError()

        # Update last_login_at
        user.last_login_at = utcnow()
        await self._session.flush()

        # F015: warm the per-user config caches so the first
        # LLM / KB / vision call after login doesn't re-decrypt the
        # Fernet-encrypted key.  ``bootstrap_all_user_caches`` covers
        # the three per-user configs (primary_model + knowledge_base +
        # image_understanding) so login is the single point where the
        # in-process caches get populated.  We intentionally do NOT
        # block the login — the next call will surface the friendly
        # "未配置" error and the frontend will route them to Settings.
        try:
            from app.services.settings_service import SettingsService
            await SettingsService(self._session).bootstrap_all_user_caches(user.id)
        except Exception as exc:  # noqa: BLE001 — best-effort
            import logging
            logging.getLogger("auth").warning(
                "bootstrap_all_user_caches failed for user_id=%s: %s",
                user.id, exc,
            )

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

    async def register(self, req: RegisterRequest) -> AuthTokenResponse:
        """Register a new user account."""
        # Validate email format
        email_re = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if not re.match(email_re, req.email):
            raise ValidationError("邮箱格式不正确")

        # Check email uniqueness
        existing = await self._user_repo.get_by_email(req.email)
        if existing:
            raise AppError(40900, "该邮箱已被注册")

        # Check passwords match (double-check, schema also validates)
        if req.password != req.confirm_password:
            raise ValidationError("两次输入的密码不一致")

        if not req.agree_terms:
            raise ValidationError("请先同意服务条款和隐私政策")

        # Generate username from email prefix
        base_username = req.email.split("@")[0].lower()
        # Only allow letters, digits, underscores, hyphens
        base_username = re.sub(r"[^a-z0-9_-]", "", base_username) or "user"

        # Ensure username uniqueness — append numeric suffix if needed
        username = base_username
        suffix = 1
        while await self._user_repo.get_by_username(username):
            username = f"{base_username}{suffix}"
            suffix += 1

        # Hash password
        password_hash_str = hash_password(req.password)

        now = utcnow()
        user = User(
            public_id=generate_public_id("user"),
            username=username,
            password_hash=password_hash_str,
            display_name=base_username,
            email=req.email,
            role="user",
            status="active",
            avatar_url=None,
            created_at=now,
            updated_at=now,
        )

        user = await self._user_repo.create(user)

        token = create_access_token(user.public_id)
        expires_in = 86400  # matches ACCESS_TOKEN_EXPIRE_MINUTES default

        return AuthTokenResponse(
            access_token=token,
            token_type="bearer",
            expires_in=expires_in,
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

    async def update_profile(
        self, user_internal_id: int, req: UpdateProfileRequest
    ) -> UserResponse:
        """Update current user's display_name, username, and/or avatar_url."""
        user = await self._user_repo.get_by_id(user_internal_id)
        if not user:
            raise InvalidCredentialsError()

        # Validate username if provided
        if req.username is not None:
            if not re.match(r"^[a-zA-Z0-9_-]{3,32}$", req.username):
                raise ValidationError("用户名只允许字母、数字、下划线、短横线，长度 3-32")
            existing = await self._user_repo.get_by_username(req.username)
            if existing and existing.id != user.id:
                raise AppError(40900, "该用户名已被使用")

        # Validate display_name if provided
        if req.display_name is not None and req.display_name.strip() == "":
            raise ValidationError("显示名称不能为空")

        now = utcnow()

        if req.display_name is not None:
            user.display_name = req.display_name
        if req.username is not None:
            user.username = req.username
        if req.avatar_url is not None:
            user.avatar_url = req.avatar_url

        user.updated_at = now
        await self._session.flush()

        # Phase 1 — Redis Cache project (Step 4): Auth Principal write-through.
        # P0 收口:任何 role / status / display_name / username / avatar_url 修改后
        # 必须把新值塞回 Redis，让下次 get_current_user 不再读到 stale DTO。
        # 用 ``register_after_commit`` 挂 hook;rollback 不触发,保证 Redis 不会
        # 领先于已提交的 MySQL 状态.
        try:
            from app.cache.domains.auth_cache import (
                AuthPrincipalDTO,
                get_auth_principal_cache,
            )
            from app.db.sync import register_after_commit

            _public_id = user.public_id
            _user_id = user.id
            _dto = AuthPrincipalDTO(
                internal_id=user.id,
                public_id=user.public_id,
                display_name=user.display_name,
                username=user.username,
                email=user.email,
                role=user.role,
                status=user.status,
                avatar_url=user.avatar_url,
            )

            async def _do_write_through(_session):
                try:
                    await get_auth_principal_cache().write_through(
                        _public_id, _dto,
                    )
                except Exception as cache_exc:  # noqa: BLE001
                    logger.warning(
                        "AuthService.update_profile: auth principal cache "
                        "write-through failed | user_id=%s | err=%s",
                        _user_id, cache_exc,
                    )

            register_after_commit(self._session, _do_write_through)
        except Exception as exc:  # noqa: BLE001 — cache failure must not fail the update
            logger.warning(
                "AuthService.update_profile: auth principal cache hook register "
                "failed | user_id=%s | err=%s",
                user.id, exc,
            )

        return UserResponse(
            user_id=user.public_id,
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            role=user.role,
            status=user.status,
            avatar_url=user.avatar_url,
            updated_at=user.updated_at.isoformat() if user.updated_at else None,
        )

    async def get_current_user(self, token: str) -> UserProfile:
        """Resolve a user from a JWT token. (Used by deps.py primarily.)"""
        from app.core.security import decode_access_token

        payload = decode_access_token(token)
        public_id = payload.get("sub")
        if not public_id:
            raise InvalidCredentialsError()

        user = await self._user_repo.get_by_public_id(public_id)
        if not user:
            raise InvalidCredentialsError()

        return UserProfile(
            id=user.public_id,
            name=user.display_name or user.username,
            role=user.role,
            username=user.username,
            email=user.email,
            status=user.status,
            avatar_url=user.avatar_url,
        )

    async def logout(self) -> None:
        """Logout — Phase 1 no-op (stateless JWT)."""
        return None


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (登录 / 注册 / JWT 签发):
#
#   链路:
#     api/v1/auth.py endpoints:
#       POST /auth/register → AuthService.register(email, password, name)
#         → 校验密码强度 / 邮箱唯一;
#         → crypto.hash_password(...) 落 user 行;
#       POST /auth/login    → AuthService.login(email, password)
#         → 查 user + crypto.verify_password;
#         → 签发 JWT (auth_provider.create_access_token);
#         → 返回 token + user_basic_profile
#       POST /auth/logout   → AuthService.logout(token) ——
#         → 走 token 黑名单(Redis)或 stateless(简单实现不维护);
#
#   后续每个 API:
#     FastAPI Depends → 解析 Authorization Bearer → 当前 user
#     → 现有请求 → services 层 (用 user_internal_id 落库)
#
# 关键约束(供开发者速查):
#   - 密码绝不进日志,**必须** crypto.hash_password;
#   - JWT secret 由 settings_service 读;严禁 hardcode;
#   - 测试用 pytest 强制使用 mock auth_provider,不走真签发;
#   - 注册失败 → 401 vs 403:用 401 表示凭证错误(更标准的 REST 语义)。
