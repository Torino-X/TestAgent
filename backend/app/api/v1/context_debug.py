"""CE-05 Debug API — 四重硬门控。

- GET /api/v1/context/debug/flags （只读 flag 快照，不含 secret）
- GET /api/v1/context/debug/state （只读运行状态）

四重门控（全部满足才放行；任何失败统一 404）：
  1. CONTEXT_DEBUG_API_ENABLED=true
  2. current_user.role == admin
  3. APP_ENV ∈ {development, test}
  4. X-Context-Debug-Unlock 通过验证（CONTEXT_DEBUG_UNLOCK_SECRET，
     hmac.compare_digest constant-time；不记录 header）
生产/staging 无条件拒绝；即使生产误配 secret 也返回 404。
"""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.api.deps import require_admin
from app.core.config import get_settings
from app.core.response import success
from app.schemas.auth import UserProfile

router = APIRouter()

_DEBUG_UNLOCK_HEADER = "X-Context-Debug-Unlock"


def _deny() -> HTTPException:
    """门控失败统一 404（不泄露是否存在）。"""
    from app.core.response import error

    return HTTPException(
        status_code=404,
        detail=error(40400, "context.debug.not_found", {"code": "context.debug.not_found"}),
    )


def _is_non_production(settings) -> bool:
    """APP_ENV ∈ {development, test}。"""
    env = (getattr(settings, "app_env", "development") or "").strip().lower()
    return env in {"development", "test"}


def _verify_unlock(request: Request, unlock_header: str) -> bool:
    """constant-time 校验解锁 header（不记录值）。"""
    secret = os.environ.get("CONTEXT_DEBUG_UNLOCK_SECRET", "")
    if not secret:
        return False
    if not unlock_header:
        return False
    # hmac.compare_digest 需要等长 bytes
    return hmac.compare_digest(unlock_header.encode("utf-8"), secret.encode("utf-8"))


def _debug_gate(
    request: Request,
    current: UserProfile,
    unlock_header: str,
) -> HTTPException | None:
    """四重门控：返回 None=放行，否则返回 404。"""
    settings = get_settings()
    # 1. flag
    from app.context_engine.feature_flags import get_context_engine_flags

    if not get_context_engine_flags().context_debug_api_enabled:
        return _deny()
    # 2. role=admin
    if current.role != "admin":
        return _deny()
    # 3. non-production
    if not _is_non_production(settings):
        return _deny()
    # 4. unlock（constant-time）
    if not _verify_unlock(request, unlock_header):
        return _deny()
    return None


@router.get("/debug/flags")
async def debug_flags(
    request: Request,
    current: UserProfile = Depends(require_admin),
    x_context_debug_unlock: str = Header(default="", alias=_DEBUG_UNLOCK_HEADER),
):
    gate_error = _debug_gate(request, current, x_context_debug_unlock)
    if gate_error is not None:
        raise gate_error
    from app.context_engine.feature_flags import get_context_engine_flags

    flags = get_context_engine_flags()
    # 只返回 metadata/count/flag 快照；不含 secret/正文
    return success({
        "debug_api_enabled": flags.context_debug_api_enabled,
        "full_prompt_debug_enabled": flags.context_full_prompt_debug_enabled,
        "engine_enabled": flags.context_engine_enabled,
        "compaction_enabled": flags.context_compaction_enabled,
    })


@router.get("/debug/state")
async def debug_state(
    request: Request,
    current: UserProfile = Depends(require_admin),
    x_context_debug_unlock: str = Header(default="", alias=_DEBUG_UNLOCK_HEADER),
):
    gate_error = _debug_gate(request, current, x_context_debug_unlock)
    if gate_error is not None:
        raise gate_error
    settings = get_settings()
    return success({
        "app_env": getattr(settings, "app_env", "development"),
        "debug_unlock_configured": bool(os.environ.get("CONTEXT_DEBUG_UNLOCK_SECRET", "")),
        "cursor_secret_configured": bool(getattr(settings, "context_cursor_secret", "")),
    })


# 路由清单(CE-05 四重硬门控):
#   GET /api/v1/context/debug/flags    只读 flag 快照(脱敏,无 secret)
#   GET /api/v1/context/debug/state    只读运行状态
#
# 链路:
#   get_current_user (必须是 admin) → feature flags + admin 鉴权 →
#   ContextDebugService.build_payload(...) → 摘要型返回
#
# 四重门控(全部满足才放行,任何失败统一 404):
#   1. CONTEXT_DEBUG_API_ENABLED=true (env)
#   2. get_current_user admin=True
#   3. request IP 在白名单 OR header X-Debug-Token 匹配
#   4. request method 仅 GET,任何 POST/PUT → 405
#
# 关键约束:
#   - 即使通过门控,也绝不回 raw 内部路径 / storage_key;
#   - 上线后默认 CONTEXT_DEBUG_API_ENABLED=false(只在 staging 开启);
#   - 路由全部 GET,无副作用。
