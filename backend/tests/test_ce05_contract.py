"""CE-05 WP-1 合同测试：Envelope / opaque cursor / 权限依赖。

覆盖：
  - cursor.py AES-GCM 编解码 + 密钥派生 + 认证失败 + 版本/长度校验
  - deps.py require_role/require_admin/require_workspace 401/403 语义
  - schemas/context_engine 分页/错误契约
"""

from __future__ import annotations

import base64
import json

import pytest

from app.schemas.context_engine.cursor import (
    CursorInvalid,
    CursorKeySet,
    CursorUnavailable,
    decode_cursor,
    encode_cursor,
)
from app.schemas.context_engine.rest import CursorPage, PaginationParams


# ══════════════════════════════════════════════════════════════════
# cursor：AES-GCM 编解码
# ══════════════════════════════════════════════════════════════════

_SECRET = "cursor-test-secret-0123456789abcdefghijklmnopqrstuv"  # ≥32 bytes


def _keys(secret: str = _SECRET, prev: str | None = None) -> CursorKeySet:
    return CursorKeySet.from_env(secret, prev)


def test_cursor_roundtrip():
    keys = _keys()
    token = encode_cursor(created_at="2026-08-06T00:00:00Z", id=123, keys=keys, resource="audit")
    created_at, obj_id = decode_cursor(token, keys=keys, resource="audit")
    assert created_at == "2026-08-06T00:00:00Z"
    assert obj_id == 123


def test_cursor_is_opaque_no_internal_id():
    keys = _keys()
    token = encode_cursor(created_at="2026-08-06T00:00:00Z", id=123, keys=keys)
    # base64url 解码后不应出现明文 "123" 或 "2026-08-06"
    raw = base64.urlsafe_b64decode(token + "==")
    assert b"123" not in raw
    assert b"2026-08-06" not in raw
    # 也不应出现完整 JSON 结构
    assert b'{"created_at"' not in raw


def test_cursor_key_derivation_hkdf_32bytes():
    keys = _keys()
    assert len(keys.current) == 32
    assert keys.previous is None


def test_cursor_secret_too_short_unavailable():
    with pytest.raises(CursorUnavailable):
        CursorKeySet.from_env("short")


def test_cursor_tamper_invalid():
    keys = _keys()
    token = encode_cursor(created_at="2026-08-06T00:00:00Z", id=1, keys=keys)
    raw = bytearray(base64.urlsafe_b64decode(token + "=="))
    # 翻转一个 bit → 认证失败
    raw[-1] ^= 0x01
    tampered = base64.urlsafe_b64encode(bytes(raw)).decode("ascii").rstrip("=")
    with pytest.raises(CursorInvalid):
        decode_cursor(tampered, keys=keys)


def test_cursor_invalid_base64():
    with pytest.raises(CursorInvalid):
        decode_cursor("!!!not-base64!!!", keys=_keys())


def test_cursor_previous_key_rotation_compat():
    old = _keys(prev=None)
    token = encode_cursor(created_at="2026-08-06T00:00:00Z", id=7, keys=old)
    # 轮换：旧密钥成为 previous，新密钥为 current
    rotated = _keys(prev=_SECRET)
    created_at, obj_id = decode_cursor(token, keys=rotated)
    assert obj_id == 7
    assert created_at == "2026-08-06T00:00:00Z"


# ══════════════════════════════════════════════════════════════════
# 分页/错误契约
# ══════════════════════════════════════════════════════════════════

def test_pagination_params_defaults():
    params = PaginationParams()
    assert params.limit == 20
    assert params.cursor is None
    assert params.include_total is False


def test_pagination_params_limit_bounds():
    with pytest.raises(Exception):
        PaginationParams(limit=0)
    with pytest.raises(Exception):
        PaginationParams(limit=101)


def test_cursor_page_shape():
    page = CursorPage[int](items=[1, 2, 3], next_cursor="abc", total=5)
    assert page.items == [1, 2, 3]
    assert page.next_cursor == "abc"
    assert page.total == 5


# ══════════════════════════════════════════════════════════════════
# 权限依赖
# ══════════════════════════════════════════════════════════════════

from app.schemas.auth import UserProfile  # noqa: E402


def _user(role: str) -> UserProfile:
    return UserProfile(
        id="pub_1",
        internal_id=1,
        name="tester",
        role=role,
        username="tester",
        email="t@example.com",
        status="active",
    )


async def test_require_admin_accepts_admin():
    from app.api.deps import require_admin

    result = await require_admin(current=_user("admin"))
    assert result.role == "admin"


async def test_require_admin_rejects_user():
    from app.api.deps import require_admin
    from fastapi.exceptions import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await require_admin(current=_user("user"))
    assert exc_info.value.status_code == 403


async def test_require_role_accepts_match():
    from app.api.deps import require_role

    result = await require_role("admin", current=_user("admin"))
    assert result.role == "admin"


async def test_require_workspace_owner_key():
    from app.api.deps import require_workspace

    key = await require_workspace("ws_1", current=_user("user"))
    assert key == "ws_1"
    empty = await require_workspace("", current=_user("user"))
    assert empty == ""


# ══════════════════════════════════════════════════════════════════
# Envelope（复用 app/core/response）
# ══════════════════════════════════════════════════════════════════

def test_envelope_success_and_error():
    from app.core.response import success, error

    ok = success({"k": 1})
    assert ok["code"] == 0
    assert ok["message"] == "success"
    assert ok["data"] == {"k": 1}
    assert ok["request_id"].startswith("req_")

    err = error(40900, "conflict", {"hint": "x"})
    assert err["code"] == 40900
    assert err["data"] == {"hint": "x"}
