"""CE-05 opaque composite cursor — AES-256-GCM 认证加密。

合同（计划 §4.2）：
- Algorithm:   AES-256-GCM
- Key:         32 bytes（env CONTEXT_CURSOR_SECRET，要求 ≥32 字节）
- Nonce:       12 random bytes per cursor
- Tag:         16 bytes
- Token Version: v1
- Wire Format: base64url(version || nonce || ciphertext || tag)
- AAD:         API resource name + cursor version
- Secret invalid/missing:   503 context.cursor.unavailable
- Decryption/auth failure:  400 context.cursor.invalid
- 密钥轮换：Current Key 写入；Previous Key 只读兼容；旧 Key 到期拒绝

内部结构：{"created_at": "ISO UTC", "id": 123}
排序：ORDER BY created_at DESC, id DESC
下一页条件：created_at < c.created_at OR (created_at = c.created_at AND id < c.id)

关键密钥派生：CONTEXT_CURSOR_SECRET 采用严格的 HKDF-SHA256 派生为恰好
32 字节；不得直接截断任意长度字符串。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

# AES-256-GCM 参数（合同固定）
_KEY_LEN = 32
_NONCE_LEN = 12
_TAG_LEN = 16
_TOKEN_VERSION = b"v1"


class CursorError(Exception):
    """Cursor 基类错误。"""

    http_status = 400
    code = "context.cursor.invalid"


class CursorUnavailable(CursorError):
    """密钥缺失/无效（未配置或 < 32 字节）。"""

    http_status = 503
    code = "context.cursor.unavailable"


class CursorInvalid(CursorError):
    """解密/认证失败/非法编码。"""

    http_status = 400
    code = "context.cursor.invalid"


@dataclass(frozen=True)
class CursorKeySet:
    """密钥集合：当前写密钥 + 可选前一个只读密钥。

    HKDF-SHA256 从 CONTEXT_CURSOR_SECRET 派生 32 字节（info 区分写/读）。
    """

    current: bytes
    previous: bytes | None = None

    @classmethod
    def from_env(cls, current_secret: str, previous_secret: str | None = None) -> "CursorKeySet":
        if not current_secret or len(current_secret.encode("utf-8")) < 32:
            raise CursorUnavailable("CONTEXT_CURSOR_SECRET 未配置或小于 32 字节")
        current = cls._derive(current_secret, b"cursor-current")
        previous = cls._derive(previous_secret, b"cursor-previous") if previous_secret else None
        return cls(current=current, previous=previous)

    @staticmethod
    def _derive(secret: str, info: bytes) -> bytes:
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=_KEY_LEN,
            salt=None,
            info=info,
        )
        return hkdf.derive(secret.encode("utf-8"))


def _resource_aad(resource: str) -> bytes:
    return (resource or "context").encode("utf-8") + b"|" + _TOKEN_VERSION


def encode_cursor(
    *,
    created_at: str,
    id: int,
    keys: CursorKeySet,
    resource: str = "context",
) -> str:
    """编码 opaque cursor：base64url(version || nonce || ciphertext || tag)。"""
    payload = json.dumps(
        {"created_at": created_at, "id": int(id)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    nonce = os.urandom(_NONCE_LEN)
    aad = _resource_aad(resource)
    ciphertext = AESGCM(keys.current).encrypt(nonce, payload, aad)
    return base64.urlsafe_b64encode(_TOKEN_VERSION + nonce + ciphertext).decode("ascii")


def decode_cursor(
    cursor: str,
    *,
    keys: CursorKeySet,
    resource: str = "context",
) -> tuple[str, int]:
    """解码 opaque cursor，返回 (created_at, id)。

    尝试当前密钥；认证失败时尝试 previous 只读密钥（轮换兼容）。
    """
    if not cursor:
        raise CursorInvalid("cursor 缺失")
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii") + b"==")
    except Exception as exc:  # noqa: BLE001
        raise CursorInvalid("cursor 编码非法") from exc

    if len(raw) < len(_TOKEN_VERSION) + _NONCE_LEN + _TAG_LEN:
        raise CursorInvalid("cursor 长度非法")
    version = raw[: len(_TOKEN_VERSION)]
    if version != _TOKEN_VERSION:
        raise CursorInvalid(f"不支持的 cursor 版本: {version!r}")
    nonce = raw[len(_TOKEN_VERSION) : len(_TOKEN_VERSION) + _NONCE_LEN]
    ciphertext = raw[len(_TOKEN_VERSION) + _NONCE_LEN :]
    aad = _resource_aad(resource)

    candidates = [keys.current] + ([keys.previous] if keys.previous else [])
    last_exc: Exception | None = None
    for key in candidates:
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
            obj = json.loads(plaintext.decode("utf-8"))
            created_at = obj.get("created_at")
            obj_id = obj.get("id")
            if not isinstance(created_at, str) or not isinstance(obj_id, int):
                raise CursorInvalid("cursor 内容结构非法")
            return created_at, obj_id
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
    raise CursorInvalid("cursor 认证失败") from last_exc


def build_next_cursor_condition(created_at: str, id: int) -> str:
    """下一页 WHERE 条件（创建者拼接到 SQL）。"""
    return (
        f"(created_at < :cursor_created_at OR "
        f"(created_at = :cursor_created_at AND id < :cursor_id))"
    )


__all__ = [
    "CursorKeySet",
    "CursorError",
    "CursorUnavailable",
    "CursorInvalid",
    "encode_cursor",
    "decode_cursor",
    "build_next_cursor_condition",
]
# auto-appended module-level note: cursor: opaque base64 cursor(分页用), 不可被前端猜解。
