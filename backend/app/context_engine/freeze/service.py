"""CE-05 Task Freeze Service — Manifest 构建与 write-once 持久化。

计划 §2.1/§2.2：
- reserved key: task_context_json.frozen_flags_manifest（独占）
- 创建任务同事务写入（Manifest 写失败 → 回滚整事务 → 任务创建失败）
- write-once：已存在且 digest 相同 → 幂等 no-op；已存在且 digest 不同 → 409
- 深度 merge：业务更新保留 reserved key
- 并发：两 Worker 同时回填只允许一个成功（无 lost update）
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.context_engine.freeze.profiles import (
    FIXED_FLAGS_DIGEST,
    LANGGRAPH_TASK_SEMANTIC_FLAGS,
    LEGACY_TASK_SEMANTIC_FLAGS,
    SCHEMA_VERSION,
    compute_flags_digest,
)

logger = logging.getLogger(__name__)

RESERVED_KEY = "frozen_flags_manifest"


class TaskFreezeError(Exception):
    """Task Freeze 基类错误。"""

    http_status = 400
    code = "context.task_freezing.error"


class TaskFreezeConflict(TaskFreezeError):
    """write-once 冲突：Manifest 已存在且 digest 不同。"""

    http_status = 409
    code = "context.task_freezing.conflict"


class TaskFreezeNotReady(TaskFreezeError):
    """新任务无 Manifest：禁止进入业务 Node。"""

    http_status = 400
    code = "context.task_freezing.not_ready"


class TaskFreezeIntegrityError(TaskFreezeError):
    """engine 与 Manifest 不一致 / digest 缺失（DATA_INTEGRITY_ERROR）。"""

    http_status = 409
    code = "context.task_freezing.data_integrity"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_manifest(
    *,
    engine: str,
    decision_reason: str,
    canary_bucket: int | None,
    workspace_key: str | None,
    context_engine_version: str,
    task_semantic_flags: dict[str, bool],
) -> dict:
    """构建 frozen manifest（flags_digest 由 canonical JSON 计算）。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "compatibility_profile": None,
        "engine": engine,
        "engine_decision_reason": decision_reason,
        "canary_bucket": canary_bucket,
        "workspace_key": workspace_key,
        "context_engine_version": context_engine_version,
        "context_policy_version": "v1",
        "security_policy_version_at_create": "v1",
        "task_semantic_flags": dict(task_semantic_flags),
        "flags_digest": compute_flags_digest(task_semantic_flags),
        "created_at": _utcnow(),
    }


def extract_manifest(task_context_json: dict | None) -> dict | None:
    """从 task_context_json 提取 frozen manifest（reserved key）。"""
    if not task_context_json or not isinstance(task_context_json, dict):
        return None
    manifest = task_context_json.get(RESERVED_KEY)
    return manifest if isinstance(manifest, dict) else None


def merge_context_json(existing: dict | None, patch: dict) -> dict:
    """深度 merge：保留 reserved key，业务 patch 只能写非 reserved 字段。"""
    base = dict(existing) if isinstance(existing, dict) else {}
    base.setdefault(RESERVED_KEY, None)
    for key, value in patch.items():
        if key == RESERVED_KEY:
            continue  # 普通更新禁止写 reserved key
        base[key] = value
    return base


def write_once_validate(manifest: dict) -> None:
    """write-once 校验：reserved key 完整性 + digest 一致性。"""
    if not isinstance(manifest.get("task_semantic_flags"), dict):
        raise TaskFreezeIntegrityError("Manifest 缺失 task_semantic_flags")
    expected = compute_flags_digest(manifest["task_semantic_flags"])
    if manifest.get("flags_digest") != expected:
        raise TaskFreezeIntegrityError("Manifest flags_digest 与 task_semantic_flags 不一致")


def resolve_engine_consistency(task_engine_type: str | None, manifest: dict) -> str:
    """校验 task.engine_type 与 manifest.engine 一致性。

    一致 → 返回 engine；不一致 → DATA_INTEGRITY_ERROR（任务 BLOCKED）。
    """
    task_engine = task_engine_type or "legacy"
    manifest_engine = manifest.get("engine") or "legacy"
    if task_engine != manifest_engine:
        raise TaskFreezeIntegrityError(
            f"engine 不一致: task.engine_type={task_engine} != manifest.engine={manifest_engine}"
        )
    return manifest_engine


def default_compatibility_flags(engine: str) -> dict[str, bool]:
    """旧任务回填时的代码内固定 task_semantic_flags（不读动态 env）。"""
    return (
        dict(LANGGRAPH_TASK_SEMANTIC_FLAGS)
        if engine == "langgraph"
        else dict(LEGACY_TASK_SEMANTIC_FLAGS)
    )


def default_compatibility_digest() -> str:
    """固定 compatibility profile digest（两套相同）。"""
    return FIXED_FLAGS_DIGEST


__all__ = [
    "RESERVED_KEY",
    "TaskFreezeError",
    "TaskFreezeConflict",
    "TaskFreezeNotReady",
    "TaskFreezeIntegrityError",
    "build_manifest",
    "extract_manifest",
    "merge_context_json",
    "write_once_validate",
    "resolve_engine_consistency",
    "default_compatibility_flags",
    "default_compatibility_digest",
]
# auto-appended module-level note: freeze service: 触发 / 解除 / 查询 freeze 的统一入口。
