"""CE-05 Task Freeze 运行时桥 — 任务路径从 DB 加载 Manifest → Resolver。

计划 §2.3 传递链：
  agent_tasks.task_context_json.frozen_flags_manifest
  → TaskScopedFeatureFlagResolver
  → LangGraph Node / Service / Tool

使用规则：
- 任务路径（有 task_id/task_public_id）：必须经本模块获取 resolver；Task-semantic
  /MIG flag 从 Manifest 读取（无 Manifest → 原子回填兼容 Profile 后读取）。
- 非任务路径（如无 task 的 chat）：走进程级 env（不属于 Task Path，门禁豁免）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.context_engine.freeze.profiles import (
    LANGGRAPH_TASK_SEMANTIC_FLAGS,
    LEGACY_TASK_SEMANTIC_FLAGS,
)
from app.context_engine.freeze.resolver import (
    TaskScopedContextFlags,
    TaskScopedFeatureFlagResolver,
)
from app.context_engine.freeze.service import (
    RESERVED_KEY,
    TaskFreezeIntegrityError,
    build_manifest,
    default_compatibility_digest,
    extract_manifest,
    resolve_engine_consistency,
    write_once_validate,
)

logger = logging.getLogger(__name__)


def build_resolver_from_manifest(manifest: dict) -> TaskScopedFeatureFlagResolver:
    """从已提取的 Manifest 构建 resolver（先校验 engine 一致性由调用方保证）。"""
    write_once_validate(manifest)
    return TaskScopedFeatureFlagResolver.from_manifest(manifest)


def build_backfill_resolver(
    task_engine_type: str | None,
    *,
    created_at: str | None = None,
) -> TaskScopedFeatureFlagResolver:
    """旧任务回填：代码内固定 compatibility profile（不读动态 env）。

    返回 resolver；调用方负责把 manifest 原子回填写库。
    """
    engine = task_engine_type or "legacy"
    flags = (
        dict(LANGGRAPH_TASK_SEMANTIC_FLAGS)
        if engine == "langgraph"
        else dict(LEGACY_TASK_SEMANTIC_FLAGS)
    )
    manifest = build_manifest(
        engine=engine,
        decision_reason="legacy_task_backfill" if engine == "legacy" else "langgraph_task_backfill",
        canary_bucket=None,
        workspace_key=None,
        context_engine_version="v2" if engine == "legacy" else "v3",
        task_semantic_flags=flags,
    )
    return TaskScopedFeatureFlagResolver.from_manifest(manifest)


async def resolve_task_resolver(
    task_engine_type: str | None,
    task_context_json: dict | None,
    *,
    task_id: int | None = None,
) -> TaskScopedFeatureFlagResolver:
    """任务路径统一入口：从 task_context_json 提取 Manifest → resolver。

    - 有 Manifest → 校验 engine 一致性 → 构建 resolver
    - 无 Manifest（旧任务）→ 固定兼容 Profile → resolver（调用方负责回填）
    """
    manifest = extract_manifest(task_context_json)
    if manifest is not None:
        resolve_engine_consistency(task_engine_type, manifest)
        return build_resolver_from_manifest(manifest)
    # 旧任务：代码内固定 profile（不回填由本函数负责；调用方在任务创建/回填流程处理）
    return build_backfill_resolver(task_engine_type)


def manifest_for_task(task_engine_type: str | None) -> dict:
    """构建旧任务回填 manifest（供调用方写入 task_context_json）。"""
    engine = task_engine_type or "legacy"
    flags = (
        dict(LANGGRAPH_TASK_SEMANTIC_FLAGS)
        if engine == "langgraph"
        else dict(LEGACY_TASK_SEMANTIC_FLAGS)
    )
    manifest = build_manifest(
        engine=engine,
        decision_reason="legacy_task_backfill" if engine == "legacy" else "langgraph_task_backfill",
        canary_bucket=None,
        workspace_key=None,
        context_engine_version="v2" if engine == "legacy" else "v3",
        task_semantic_flags=flags,
    )
    write_once_validate(manifest)
    return manifest


__all__ = [
    "build_resolver_from_manifest",
    "build_backfill_resolver",
    "resolve_task_resolver",
    "manifest_for_task",
]
# auto-appended module-level note: freeze runtime: 冻结状态机(在 freeze 中 → 在 rollout 中 → released)。
