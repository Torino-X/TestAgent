"""Symmetric encryption helpers for storing secrets at rest.

Uses Fernet (AES-128-CBC + HMAC-SHA256) keyed off the application
``SECRET_KEY`` setting.  Suitable for low-throughput secrets such as
LLM API keys and knowledge-base API keys — NOT for high-volume data.

The derived key is deterministic per ``SECRET_KEY``, so the same key can
encrypt and decrypt across processes.  Rotate by changing ``SECRET_KEY``
and re-encrypting stored ciphertexts.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


class CryptoError(Exception):
    """Raised when encryption / decryption fails."""


def _derive_key() -> bytes:
    """Derive a 32-byte url-safe-base64 Fernet key from SECRET_KEY.

    SHA-256 produces 32 raw bytes; Fernet requires a urlsafe-base64
    encoded 32-byte key.
    """
    secret = get_settings().secret_key or "change_me"
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_derive_key())


def encrypt_api_key(plain: str) -> str:
    """Encrypt a plaintext API key.  Returns a Fernet token (base64 string)."""
    if plain is None:
        raise CryptoError("Cannot encrypt None")
    if not isinstance(plain, str):
        plain = str(plain)
    if plain == "":
        return ""
    try:
        return _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")
    except Exception as exc:
        raise CryptoError(f"Encryption failed: {exc}") from exc


def decrypt_api_key(cipher: str) -> str:
    """Decrypt a Fernet token back to plaintext API key.

    Returns the empty string when cipher is empty.  Raises CryptoError
    on tampered or rotated keys.
    """
    if not cipher:
        return ""
    if not isinstance(cipher, str):
        cipher = str(cipher)
    try:
        return _fernet().decrypt(cipher.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise CryptoError(
            "Decryption failed: invalid token (key rotated or data tampered)"
        ) from exc
    except Exception as exc:
        raise CryptoError(f"Decryption failed: {exc}") from exc


def mask_api_key(plain: str, keep: int = 4) -> str:
    """Produce a masked representation of an API key.

    Keeps the first and last ``keep`` characters; replaces the rest with
    "****".  Returns "" for empty / None input.

    Example: "sk-abcdefghijklmnop" → "sk-ab********mnop"
    """
    if not plain:
        return ""
    if len(plain) <= keep * 2 + 2:
        # Too short to safely mask — show first 2 + asterisks + last 2.
        if len(plain) <= 4:
            return "****"
        return plain[:2] + "****" + plain[-2:]
    return plain[:keep] + "****" + plain[-keep:]# crypto:对称加密工具(Fernet + AES-128-CBC + HMAC-SHA256);基于 SECRET_KEY 派生,用于 LLM/知识库密钥等低频敏感字段。
