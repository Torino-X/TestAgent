"""CE-05 TaskScopedFeatureFlagResolver — 任务级 Flag 解析器。

计划 §2.3 运行时执行合同：
- 有 Task Manifest：Task-semantic/MIG Flag → 从 Manifest 读取
- 无 Task Manifest 且属新任务创建：禁止进入业务 Node（任务创建失败）
- 旧任务（无 Manifest）：先原子回填固定 Compatibility Profile，再从 Manifest 读取
- Runtime Kill Switch：始终从当前运行环境读取（实时）
- Operational Flag：始终从当前运行环境读取（实时）

禁止业务调用点直接读动态 env 求值 Task-semantic/MIG Flag。
"""

from __future__ import annotations

import logging
from typing import Any

from app.context_engine.freeze.profiles import (
    FROZEN_FLAG_KEYS,
    LANGGRAPH_TASK_SEMANTIC_FLAGS,
    LEGACY_TASK_SEMANTIC_FLAGS,
)

logger = logging.getLogger(__name__)

# Operational flags（C 类）：不入 Manifest，始终实时读 env
_OPERATIONAL_FLAGS = {
    "CONTEXT_INDEX_WORKER_ENABLED",
    "CONTEXT_FULL_PROMPT_DEBUG_ENABLED",
    "CONTEXT_DEBUG_API_ENABLED",
}

# Kill Switch flags（B 类）：不入 Manifest，始终实时读 env
_KILL_SWITCH_FLAGS = {
    "CONTEXT_ENGINE_ENABLED",
}


def _env_flag(name: str) -> bool:
    """从当前运行环境实时读取单个 flag（Kill Switch / Operational）。"""
    value = __import__("os").environ.get(name, "").strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    return False


class TaskScopedContextFlags:
    """任务级上下文 flag 视图：Manifest（冻结）+ 实时来源（Kill Switch/Operational）。

    构造后不可变；业务调用点必须经本对象读取 Task-semantic/MIG Flag。
    """

    def __init__(
        self,
        *,
        engine: str,
        task_semantic_flags: dict[str, bool],
        manifest_digest: str,
    ) -> None:
        if engine not in ("legacy", "langgraph"):
            raise ValueError(f"无效 engine: {engine!r}")
        missing = FROZEN_FLAG_KEYS - set(task_semantic_flags.keys())
        if missing:
            raise ValueError(f"Manifest task_semantic_flags 缺失 {len(missing)} 项: {sorted(missing)}")
        self._engine = engine
        self._task_semantic_flags = dict(task_semantic_flags)
        self._manifest_digest = manifest_digest

    @property
    def engine(self) -> str:
        return self._engine

    @property
    def manifest_digest(self) -> str:
        return self._manifest_digest

    def is_task_semantic_flag(self, name: str) -> bool:
        return name in FROZEN_FLAG_KEYS

    def get(self, name: str) -> bool:
        """解析单个 flag：
        - Task-semantic/MIG → Manifest（冻结）
        - Kill Switch / Operational → 实时 env
        - 未知 flag → 默认 False（记录 warning）
        """
        if name in FROZEN_FLAG_KEYS:
            return bool(self._task_semantic_flags.get(name, False))
        # Kill Switch / Operational / 未知 → 实时
        if name in _KILL_SWITCH_FLAGS or name in _OPERATIONAL_FLAGS:
            return _env_flag(name)
        logger.warning("TaskScopedContextFlags 未知 flag: %s", name)
        return False


class TaskScopedFeatureFlagResolver:
    """从 Manifest 构造任务级 flag 解析器（集中 Resolver，方案 B）。

    使用规则：
    - 新任务创建时：必须提供 frozen manifest（缺失 → 禁止进入业务 Node）
    - 旧任务回填后：从 Manifest 读取
    - Kill Switch / Operational：始终经 TaskScopedContextFlags 实时读取
    """

    def __init__(self, context_flags: TaskScopedContextFlags) -> None:
        self._flags = context_flags

    @classmethod
    def from_manifest(cls, manifest: dict) -> "TaskScopedFeatureFlagResolver":
        engine = manifest.get("engine") or "legacy"
        task_flags = manifest.get("task_semantic_flags") or {}
        digest = manifest.get("flags_digest") or ""
        # 兼容旧任务：缺字段 → 用代码内固定 profile 语义（回填已写入）
        if not task_flags:
            task_flags = (
                LANGGRAPH_TASK_SEMANTIC_FLAGS if engine == "langgraph" else LEGACY_TASK_SEMANTIC_FLAGS
            )
        return cls(TaskScopedContextFlags(
            engine=engine, task_semantic_flags=task_flags, manifest_digest=digest
        ))

    @property
    def engine(self) -> str:
        return self._flags.engine

    def evaluate(self, name: str) -> bool:
        """业务调用点统一读取入口（替代直接读 env/feature_flags 实例）。"""
        return self._flags.get(name)

    def evaluate_all_task_semantic(self) -> dict[str, bool]:
        return {
            k: bool(self._flags.get(k))
            for k in sorted(FROZEN_FLAG_KEYS)
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TaskScopedFeatureFlagResolver engine={self._flags.engine}>"


__all__ = [
    "TaskScopedContextFlags",
    "TaskScopedFeatureFlagResolver",
    "FROZEN_FLAG_KEYS",
]
# auto-appended module-level note: freeze resolver: 解析用户当前生效的 freeze profile 集合。
