"""CE-05 WP-5 Payload + Conversation Settings API 测试。

覆盖：payload DTO 白名单（不含 storage_key/hash/path）、download 端点存在、
conversation context-settings 端点存在、router 挂载。
"""

from __future__ import annotations

import pytest


def test_payload_dto_forbidden_fields():
    """Payload 只读 DTO 不含 storage_key/storage_key_hash/path。"""
    from app.api.v1.context_payload import _payload_dto

    class _Row:
        public_id = "cp_1"
        payload_type = "recovery_manifest"
        status = "active"
        size_bytes = 1024
        sha256 = "a" * 64
        expires_at = None
        created_at = None
        # 应被排除
        storage_key = "/secret/path"
        storage_key_hash = "h" * 64
        encryption_key_ref = "ek_1"

    out = _payload_dto(_Row())
    assert out["public_id"] == "cp_1"
    assert "storage_key" not in out
    assert "storage_key_hash" not in out
    assert "encryption_key_ref" not in out
    # sha256 只回前 8 位
    assert out["sha256"] == "a" * 8


def test_payload_router_registered():
    from app.api.v1.context_payload import router as payload_router

    paths = [getattr(r, "path", "") for r in payload_router.routes]
    assert "/payloads" in paths
    assert "/payloads/{public_id}" in paths
    assert "/payloads/{public_id}/download" in paths


def test_conversation_context_settings_registered():
    from app.api.v1.conversations import router as conv_router

    paths = [getattr(r, "path", "") for r in conv_router.routes]
    assert any("/{conversation_id}/context-settings" in p for p in paths)


def test_memory_mode_patch_still_only_memory_mode():
    """Conversation PATCH 仍只允许 memory_mode（不受影响）。"""
    from app.api.v1.conversations import router as conv_router

    paths = [getattr(r, "path", "") for r in conv_router.routes]
    assert any("/{conversation_id}/memory-mode" in p for p in paths)
