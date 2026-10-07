"""ContextPayload storage_key 哈希与唯一性辅助。

设计说明（差异于设计文档 02 §16）：
- 设计原用 UNIQUE(storage_backend, storage_key)，storage_key VARCHAR(1000)
  在 utf8mb4 下与 UNIQUE 索引合计超过 InnoDB 3072 字节限制。
- 方案：新增 storage_key_hash CHAR(64)（SHA-256 of f"{backend}:{key}"），
  UNIQUE(storage_backend, storage_key_hash)。真实 storage_key 仍完整存储
  （storage_key 列），不丢失原始值。
- 冲突二次校验：唯一约束由 hash 承担；写入时若 hash 冲突但 key 不同
  （理论 SHA-256 碰撞），由 owner + storage_key 二次校验拒绝。
"""

from __future__ import annotations

import hashlib


def compute_storage_key_hash(storage_backend: str, storage_key: str) -> str:
    """计算 storage_key_hash = SHA-256(f"{backend}:{key}")。

    绑定 backend 避免跨 backend 的 key 冲突被误判为同一 payload。
    """
    raw = f"{storage_backend}:{storage_key}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_storage_key_uniqueness(
    *,
    storage_backend: str,
    storage_key: str,
    owner_user_id: int,
    existing_owner_user_id: int | None,
    existing_storage_key: str | None,
) -> bool:
    """写入前的唯一性二次校验。

    - 同一 owner + 同一 storage_key → 允许（幂等覆盖）；
    - 不同 owner 同 storage_key → 拒绝（owner scope 隔离）；
    - hash 冲突但 storage_key 不同 → 拒绝（SHA-256 碰撞防御）。
    """
    if existing_owner_user_id is None or existing_storage_key is None:
        return True  # 无现有行
    if existing_owner_user_id != owner_user_id:
        return False  # 跨 owner 冲突
    return existing_storage_key == storage_key  # 同 owner 必须同 key
# auto-appended module-level note: payload key: storage_key + content_hash 生成与解析。
