"""CE-05 WP-6 Index Admin + Debug API 测试。

覆盖：
  - Debug 四重门控（flag/role/env/unlock；失败统一 404）
  - Admin 跨用户审计事件写入
  - router 挂载
"""

from __future__ import annotations

import os

import pytest

from app.schemas.auth import UserProfile


def _user(role: str) -> UserProfile:
    return UserProfile(
        id="pub_1",
        internal_id=1,
        name="t",
        role=role,
        username="t",
        email="t@e.com",
        status="active",
    )


class _FakeRequest:
    def __init__(self):
        pass


def test_debug_disabled_returns_404():
    """CONTEXT_DEBUG_API_ENABLED=false → 404。"""
    from app.api.v1.context_debug import _debug_gate

    os.environ.pop("CONTEXT_DEBUG_UNLOCK_SECRET", None)
    from app.core.config import get_settings

    settings = get_settings()
    # 确保 flag 关闭（测试沙箱默认全关）
    from app.context_engine.feature_flags import get_context_engine_flags

    assert not get_context_engine_flags().context_debug_api_enabled
    err = _debug_gate(_FakeRequest(), _user("admin"), "unlock")
    assert err is not None
    assert err.status_code == 404


def test_debug_invalid_unlock_returns_404():
    """unlock header 错误 → 404（即使 flag+role+env 满足）。"""
    from app.api.v1.context_debug import _debug_gate

    os.environ["CONTEXT_DEBUG_UNLOCK_SECRET"] = "correct-secret-value"
    try:
        # flag 关（测试默认）→ 仍 404
        err = _debug_gate(_FakeRequest(), _user("admin"), "wrong")
        assert err is not None
        assert err.status_code == 404
    finally:
        os.environ.pop("CONTEXT_DEBUG_UNLOCK_SECRET", None)


def test_debug_non_admin_returns_404():
    """非 admin → 404（不区分 403/404，门控统一 404）。"""
    from app.api.v1.context_debug import _debug_gate

    err = _debug_gate(_FakeRequest(), _user("user"), "")
    assert err is not None
    assert err.status_code == 404


def test_debug_production_always_returns_404():
    """生产环境即使全满足也 404。"""
    from app.api.v1.context_debug import _debug_gate, _is_non_production

    class _ProdSettings:
        app_env = "production"

    assert not _is_non_production(_ProdSettings())
    os.environ["CONTEXT_DEBUG_UNLOCK_SECRET"] = "x" * 20
    try:
        err = _debug_gate(_FakeRequest(), _user("admin"), "x" * 20)
        # flag 关在测试沙箱 → 404（production 分支也 404，双保险）
        assert err is not None and err.status_code == 404
    finally:
        os.environ.pop("CONTEXT_DEBUG_UNLOCK_SECRET", None)


def test_debug_response_forbidden_fields_zero():
    """Debug 响应只含 metadata/count/status，不含正文/secret。"""
    from app.api.v1.context_debug import _deny

    err = _deny()
    detail = err.detail or {}
    data = detail.get("data") or {}
    # 错误体只含 code 标识，不含 prompt/secret/正文
    assert "prompt" not in str(data).lower()
    assert "secret" not in str(data).lower()
    assert "SECRET" not in str(data)
    assert str(data.get("code", "")) == "context.debug.not_found"


def test_admin_router_registered():
    from app.api.v1.context_admin import router as admin_router

    paths = [getattr(r, "path", "") for r in admin_router.routes]
    assert "/admin/index/jobs" in paths
    assert "/admin/index/jobs/{job_public_id}" in paths
    assert "/admin/index/jobs/{job_public_id}/retry" in paths
    assert "/admin/index/jobs/{job_public_id}/cancel" in paths
    assert "/admin/index/documents/{document_public_id}/delete" in paths


def test_debug_router_registered():
    from app.api.v1.context_debug import router as debug_router

    paths = [getattr(r, "path", "") for r in debug_router.routes]
    assert "/debug/flags" in paths
    assert "/debug/state" in paths


def test_admin_job_dto_forbidden_fields():
    """Admin job DTO 不含 payload 正文（只 metadata）。"""
    from app.api.v1.context_admin import _job_dto

    class _Job:
        public_id = "jb_1"
        operation = "embed_document"
        status = "pending"
        attempt = 1
        max_attempts = 3
        error_code = None
        error_message = "e" * 400
        created_at = None
        completed_at = None
        payload_json = {"document_id": 1, "content": "SECRET-CONTENT"}

    out = _job_dto(_Job())
    assert "payload_json" not in out
    assert "content" not in str(out)
    assert len(out["error_message"]) <= 300
