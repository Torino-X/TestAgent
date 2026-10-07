"""CE-05 Task Freeze 包：Manifest 构建 + 运行时 Resolver + 固定 Profile。

- profiles.py：23 项固定 task_semantic_flags + 固定 digest
- resolver.py：TaskScopedContextFlags / TaskScopedFeatureFlagResolver
- service.py：Manifest 构建 / write-once / 深度 merge / engine 一致性
"""

from app.context_engine.freeze.profiles import (
    FIXED_FLAGS_DIGEST,
    FROZEN_FLAG_KEYS,
    LANGGRAPH_PROFILE,
    LANGGRAPH_TASK_SEMANTIC_FLAGS,
    LEGACY_PROFILE,
    LEGACY_TASK_SEMANTIC_FLAGS,
    SCHEMA_VERSION,
    compute_flags_digest,
)
from app.context_engine.freeze.resolver import (
    TaskScopedContextFlags,
    TaskScopedFeatureFlagResolver,
)
from app.context_engine.freeze.runtime import (
    build_backfill_resolver,
    build_resolver_from_manifest,
    manifest_for_task,
    resolve_task_resolver,
)
from app.context_engine.freeze.service import (
    RESERVED_KEY,
    TaskFreezeConflict,
    TaskFreezeError,
    TaskFreezeIntegrityError,
    TaskFreezeNotReady,
    build_manifest,
    default_compatibility_digest,
    default_compatibility_flags,
    extract_manifest,
    merge_context_json,
    resolve_engine_consistency,
    write_once_validate,
)

__all__ = [
    "FIXED_FLAGS_DIGEST",
    "FROZEN_FLAG_KEYS",
    "LANGGRAPH_PROFILE",
    "LANGGRAPH_TASK_SEMANTIC_FLAGS",
    "LEGACY_PROFILE",
    "LEGACY_TASK_SEMANTIC_FLAGS",
    "RESERVED_KEY",
    "SCHEMA_VERSION",
    "TaskFreezeConflict",
    "TaskFreezeError",
    "TaskFreezeIntegrityError",
    "TaskFreezeNotReady",
    "TaskScopedContextFlags",
    "TaskScopedFeatureFlagResolver",
    "build_manifest",
    "build_backfill_resolver",
    "build_resolver_from_manifest",
    "compute_flags_digest",
    "default_compatibility_digest",
    "default_compatibility_flags",
    "extract_manifest",
    "manifest_for_task",
    "merge_context_json",
    "resolve_engine_consistency",
    "resolve_task_resolver",
    "write_once_validate",
]
# auto-appended module-level note: freeze 子包: ContextEngine 配置冻结 / rollout 控制入口。
