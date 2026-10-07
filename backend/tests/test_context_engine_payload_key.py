"""CE-01 整改：storage_key_hash 设计差异测试。

覆盖：相同 key / 不同用户同 key / hash 冲突模拟 / 超长 key / 重复写 / 删除重建。
"""

from __future__ import annotations

import pytest

from app.context_engine.adapters.payload_key import (
    compute_storage_key_hash,
    validate_storage_key_uniqueness,
)


def test_same_key_same_hash_deterministic():
    h1 = compute_storage_key_hash("tool_output", "storage/path/1.txt")
    h2 = compute_storage_key_hash("tool_output", "storage/path/1.txt")
    assert h1 == h2
    assert len(h1) == 64


def test_different_key_different_hash():
    h1 = compute_storage_key_hash("tool_output", "path/1.txt")
    h2 = compute_storage_key_hash("tool_output", "path/2.txt")
    assert h1 != h2


def test_same_key_different_backend_different_hash():
    """同一 key 不同 backend 哈希不同（避免跨 backend 误判）。"""
    h1 = compute_storage_key_hash("tool_output", "path/1.txt")
    h2 = compute_storage_key_hash("ocr_output", "path/1.txt")
    assert h1 != h2


def test_different_users_same_key_denied():
    """不同用户同 storage_key → 拒绝（owner scope 隔离）。"""
    assert validate_storage_key_uniqueness(
        storage_backend="tool_output",
        storage_key="path/1.txt",
        owner_user_id=1,
        existing_owner_user_id=2,
        existing_storage_key="path/1.txt",
    ) is False


def test_same_user_same_key_allowed():
    """同一用户同 storage_key → 允许（幂等覆盖）。"""
    assert validate_storage_key_uniqueness(
        storage_backend="tool_output",
        storage_key="path/1.txt",
        owner_user_id=1,
        existing_owner_user_id=1,
        existing_storage_key="path/1.txt",
    ) is True


def test_hash_conflict_with_different_key_denied():
    """hash 冲突但 storage_key 不同 → 拒绝（SHA-256 碰撞防御）。"""
    # 模拟 hash 冲突：force 现有 hash 与当前 key 的 hash 相同但 key 不同
    assert validate_storage_key_uniqueness(
        storage_backend="tool_output",
        storage_key="current-key",
        owner_user_id=1,
        existing_owner_user_id=1,
        existing_storage_key="different-key",  # 同 owner 但 key 不同 → 冲突
    ) is False


def test_long_storage_key_hash_stable():
    """超长 storage_key（>1000 chars）仍可哈希。"""
    long_key = "x" * 2000
    h = compute_storage_key_hash("tool_output", long_key)
    assert len(h) == 64
    assert compute_storage_key_hash("tool_output", long_key) == h


def test_no_existing_row_allows():
    assert validate_storage_key_uniqueness(
        storage_backend="tool_output",
        storage_key="path/1.txt",
        owner_user_id=1,
        existing_owner_user_id=None,
        existing_storage_key=None,
    ) is True


def test_delete_recreate_same_key_allowed():
    """删除重建：删除后无现有行 → 允许同 key 再次写入。"""
    # 删除后 existing 为 None（软删除或物理删除），允许重建
    assert validate_storage_key_uniqueness(
        storage_backend="tool_output",
        storage_key="path/1.txt",
        owner_user_id=1,
        existing_owner_user_id=None,
        existing_storage_key=None,
    ) is True
