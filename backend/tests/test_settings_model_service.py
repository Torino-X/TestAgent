"""Tests for SettingsService model-config methods — F015 per-user.

These tests verify that:
- model_configs is read/written via the real DB path (mocked at the
  session boundary, but exercised end-to-end through SettingsService).
- API keys are encrypted at rest and decrypted on read.
- ``build_llm_config_provider`` returns either a real
  ``LLMConfigProvider`` or an ``LLMNotConfiguredMarker`` (no env
  fallback in F015).
- ``test_model_connection`` returns success=False with a friendly
  message when the user has no model_configs row.
- ``get_model_settings`` returns ``configured=False`` for an
  unconfigured user.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.crypto import (
    CryptoError,
    decrypt_api_key,
    encrypt_api_key,
    mask_api_key,
)
from app.core.llm_config_cache import llm_config_cache
from app.core.llm_not_configured import LLMNotConfiguredMarker
from app.services.settings_service import (
    LLMConfigProvider,
    SettingsService,
)


# ── Crypto primitives ──────────────────────────────────────────────


def test_encrypt_decrypt_roundtrip():
    plain = "sk-" + "1234567890abcdefghij"
    cipher = encrypt_api_key(plain)
    assert cipher != plain
    assert decrypt_api_key(cipher) == plain


def test_decrypt_invalid_token_raises_crypto_error():
    with pytest.raises(CryptoError):
        decrypt_api_key("not-a-fernet-token")


def test_encrypt_empty_returns_empty():
    assert encrypt_api_key("") == ""


def test_mask_api_key_keeps_head_and_tail():
    masked = mask_api_key("sk-abcdefghijklmnop")
    # Keeps first 4 chars and last 4 chars, with **** in between.
    assert masked.startswith("sk-a")
    assert masked.endswith("mnop")
    assert "****" in masked


def test_mask_api_key_short_input():
    assert mask_api_key("") == ""
    assert mask_api_key("ab") == "****"


# ── SettingsService with mocked DB ─────────────────────────────────


def _make_settings_service(model_config=None):
    """Build a SettingsService with a mock async session.

    The mock repository's ``get_active_for_user`` returns
    ``model_config`` for ANY user_id — individual tests can override
    the side_effect to verify the call is properly user-scoped.
    """
    session = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()

    svc = SettingsService(session)
    mock_repo = MagicMock()
    mock_repo.get_active_for_user = AsyncMock(return_value=model_config)
    mock_repo.upsert_for_user = AsyncMock(return_value=model_config)
    svc._model_repo = mock_repo
    return svc, session, mock_repo


def _make_model_config(
    *,
    public_id="model_test",
    user_id=42,
    api_base_url="https://api.example.com/v1",
    api_key_plain = "sk-" + "real-key-xyz",
    model_name="qwen-test",
    timeout_seconds=120,
    enable_thinking=False,
    supports_vision=False,
    is_default=True,
):
    return SimpleNamespace(
        public_id=public_id,
        user_id=user_id,
        capability_type="chat",
        config_name="默认模型配置",
        provider="openai-compatible",
        api_base_url=api_base_url,
        api_key_encrypted=encrypt_api_key(api_key_plain),
        model_name=model_name,
        temperature=None,
        max_tokens=None,
        timeout_seconds=timeout_seconds,
        enable_thinking=enable_thinking,
        supports_vision=supports_vision,
        is_default=is_default,
        status="active",
        updated_at=None,
    )


# ── get_model_settings ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_model_settings_no_row_returns_unconfigured():
    svc, _, _ = _make_settings_service(model_config=None)
    result = await svc.get_model_settings(user_id=42)
    assert result["configured"] is False
    assert result["api_base_url"] == ""
    assert result["api_key_masked"] == ""
    assert result["model_name"] == ""


@pytest.mark.asyncio
async def test_get_model_settings_with_row_masks_api_key():
    cfg = _make_model_config(api_key_plain = "sk-" + "secret-abc123def456")
    svc, _, _ = _make_settings_service(model_config=cfg)

    result = await svc.get_model_settings(user_id=42)
    assert result["configured"] is True
    assert result["api_base_url"] == "https://api.example.com/v1"
    assert result["model_name"] == "qwen-test"
    # The masked key must not contain the plaintext.
    assert "sk-secret-abc123def456" not in result["api_key_masked"]
    assert "****" in result["api_key_masked"]


# ── update_model_settings ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_model_settings_encrypts_api_key():
    cfg = _make_model_config(api_key_plain = "sk-" + "old-key")
    svc, session, mock_repo = _make_settings_service(model_config=cfg)

    new_plain = "sk-" + "brand-new-key-9876"
    data = {
        "api_base_url": "https://new.api/v1",
        "model_name": "new-model",
        "api_key": new_plain,
        "timeout_seconds": 180,
        "enable_thinking": True,
    }

    async def _fake_upsert(**kwargs):
        cfg.api_base_url = kwargs["api_base_url"]
        cfg.api_key_encrypted = kwargs["api_key_encrypted"]
        cfg.model_name = kwargs["model_name"]
        cfg.timeout_seconds = kwargs["timeout_seconds"]
        cfg.enable_thinking = kwargs["enable_thinking"]
        return cfg

    mock_repo.upsert_for_user = AsyncMock(side_effect=_fake_upsert)

    result = await svc.update_model_settings(data, user_id=42)

    # The repo was called with the encrypted (not plaintext) form.
    call_kwargs = mock_repo.upsert_for_user.call_args.kwargs
    assert call_kwargs["user_id"] == 42  # F015: user_id is int
    assert call_kwargs["api_base_url"] == "https://new.api/v1"
    assert call_kwargs["api_key_encrypted"] != new_plain
    assert decrypt_api_key(call_kwargs["api_key_encrypted"]) == new_plain
    assert new_plain not in result["api_key_masked"]
    assert result["timeout_seconds"] == 180
    assert result["enable_thinking"] is True


@pytest.mark.asyncio
async def test_update_model_settings_empty_api_key_preserves_existing():
    """Empty api_key in update payload must not overwrite the stored key."""
    cfg = _make_model_config(api_key_plain = "sk-" + "keep-me")
    svc, _, mock_repo = _make_settings_service(model_config=cfg)

    await svc.update_model_settings(
        {"api_base_url": "https://x", "model_name": "m", "api_key": ""},
        user_id=42,
    )
    call_kwargs = mock_repo.upsert_for_user.call_args.kwargs
    # Empty string passed through; the repository is responsible for
    # skipping the update when api_key_encrypted == "".
    assert call_kwargs["api_key_encrypted"] == ""


# ── build_llm_config_provider ───────────────────────────────────────


@pytest.mark.asyncio
async def test_build_llm_config_provider_decrypts_correctly():
    cfg = _make_model_config(
        api_base_url="https://api.test/v1",
        api_key_plain = "sk-" + "the-real-key",
        model_name="gpt-x",
        timeout_seconds=200,
        enable_thinking=True,
    )
    svc, _, _ = _make_settings_service(model_config=cfg)

    # Make sure no stale cache entry from a prior test interferes.
    await llm_config_cache.clear()

    provider = await svc.build_llm_config_provider(user_id=42)
    assert isinstance(provider, LLMConfigProvider)
    assert provider.api_url == "https://api.test/v1"
    assert provider.api_key == "sk-the-real-key"  # decrypted
    assert provider.model_name == "gpt-x"
    assert provider.timeout == 200
    assert provider.enable_thinking is True
    assert provider.get_effective_api_key() == "sk-the-real-key"


@pytest.mark.asyncio
async def test_build_llm_config_provider_returns_marker_when_no_row():
    """F015: no row for the user → LLMNotConfiguredMarker, NOT env fallback."""
    svc, _, _ = _make_settings_service(model_config=None)
    await llm_config_cache.clear()

    provider = await svc.build_llm_config_provider(user_id=99)
    assert isinstance(provider, LLMNotConfiguredMarker)
    assert provider.is_unconfigured is True
    assert provider.api_url == ""
    assert provider.api_key == ""


@pytest.mark.asyncio
async def test_build_llm_config_provider_invalid_user_id_returns_marker():
    """Non-integer / zero user_id must NOT crash; it returns the marker."""
    svc, _, _ = _make_settings_service(model_config=None)
    await llm_config_cache.clear()

    for bad in (0, -1, None):
        provider = await svc.build_llm_config_provider(user_id=bad)
        assert isinstance(provider, LLMNotConfiguredMarker), f"bad={bad}"


@pytest.mark.asyncio
async def test_build_llm_config_provider_per_user_isolation():
    """User A's config must not leak into user B's call (F015 invariant)."""
    # Repo returns cfg only for user_id=42; None for others.
    cfg_a = _make_model_config(
        user_id=42, api_base_url="https://a.api/v1", api_key_plain = "sk-" + "A"
    )
    svc = MagicMock()
    svc._model_repo = MagicMock()
    svc._model_repo.get_active_for_user = AsyncMock(
        side_effect=lambda uid: cfg_a if uid == 42 else None
    )
    # Re-wrap as a real SettingsService instance
    real_svc = SettingsService(MagicMock())
    real_svc._model_repo = svc._model_repo
    await llm_config_cache.clear()

    provider_a = await real_svc.build_llm_config_provider(user_id=42)
    provider_b = await real_svc.build_llm_config_provider(user_id=99)

    assert isinstance(provider_a, LLMConfigProvider)
    assert provider_a.api_url == "https://a.api/v1"
    assert provider_a.api_key == "sk-A"
    assert isinstance(provider_b, LLMNotConfiguredMarker)


@pytest.mark.asyncio
async def test_build_llm_config_provider_handles_decrypt_failure():
    """A ciphertext that can't be decrypted must surface marker, not crash."""
    bad_cfg = SimpleNamespace(
        public_id="x",
        user_id=42,
        api_base_url="https://api.test/v1",
        api_key_encrypted="tampered-token",  # not valid Fernet
        model_name="m",
        timeout_seconds=120,
        enable_thinking=False,
        supports_vision=False,
    )
    svc, _, _ = _make_settings_service(model_config=bad_cfg)
    await llm_config_cache.clear()

    provider = await svc.build_llm_config_provider(user_id=42)
    assert isinstance(provider, LLMNotConfiguredMarker)
    assert provider.is_unconfigured is True


# ── test_model_connection ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_model_connection_no_row_returns_friendly_message():
    svc, _, _ = _make_settings_service(model_config=None)
    await llm_config_cache.clear()
    result = await svc.test_model_connection(user_id=42)
    assert result["success"] is False
    assert "尚未配置模型" in result["message"]


# ── bootstrap_user_config (login hook) ──────────────────────────────


@pytest.mark.asyncio
async def test_bootstrap_user_config_warms_cache():
    cfg = _make_model_config(api_key_plain = "sk-" + "bootstrap")
    svc, _, _ = _make_settings_service(model_config=cfg)
    await llm_config_cache.clear()

    provider = await svc.bootstrap_user_config(user_id=42)
    assert isinstance(provider, LLMConfigProvider)
    assert provider.api_key == "sk-bootstrap"

    # Subsequent get_or_load on the cache should return the same object
    # without re-reading the DB.
    cached = await llm_config_cache.get_or_load(
        user_id=42, loader=lambda uid: None
    )
    assert cached is provider


@pytest.mark.asyncio
async def test_bootstrap_user_config_no_row_returns_none():
    svc, _, _ = _make_settings_service(model_config=None)
    await llm_config_cache.clear()

    provider = await svc.bootstrap_user_config(user_id=42)
    assert provider is None
    # Cache should NOT cache None.
    assert 42 not in llm_config_cache.cached_user_ids()


@pytest.mark.asyncio
async def test_invalidate_clears_user_entry():
    cfg = _make_model_config()
    svc, _, _ = _make_settings_service(model_config=cfg)
    await llm_config_cache.clear()
    await svc.bootstrap_user_config(user_id=42)
    assert 42 in llm_config_cache.cached_user_ids()
    await llm_config_cache.invalidate(42)
    assert 42 not in llm_config_cache.cached_user_ids()
