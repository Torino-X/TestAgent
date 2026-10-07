"""CE-05 Task Freeze profiles and task-semantic snapshot helpers.

The all-false profiles below are compatibility profiles for historical tasks
that predate the manifest. New tasks freeze their effective runtime flags at
creation time via :func:`snapshot_task_semantic_flags`.
"""

from __future__ import annotations

import hashlib
import json

# 23 项 Task-semantic Flags（A 类，创建时冻结）— 全 False 固定值
LEGACY_TASK_SEMANTIC_FLAGS = {
    "CONTEXT_ENGINE_AGENT_ENABLED": False,
    "CONTEXT_TOOL_OUTPUT_GOVERNANCE_ENABLED": False,
    "CONTEXT_MEMORY_READ_ENABLED": False,
    "CONTEXT_MEMORY_WRITE_ENABLED": False,
    "CONTEXT_MEMORY_AUTO_EXTRACT_ENABLED": False,
    "CONTEXT_MEMORY_AUTO_ACTIVATE_ENABLED": False,
    "CONTEXT_RETRIEVAL_ENABLED": False,
    "CONTEXT_LEXICAL_RETRIEVAL_ENABLED": False,
    "CONTEXT_DENSE_RETRIEVAL_ENABLED": False,
    "CONTEXT_HYBRID_FUSION_ENABLED": False,
    "CONTEXT_RERANK_ENABLED": False,
    "CONTEXT_COMPACTION_ENABLED": False,
    "CONTEXT_CONVERSATION_COMPACTION_ENABLED": False,
    "CONTEXT_AGENT_LOOP_COMPACTION_ENABLED": False,
    "CONTEXT_FULL_REPLACE_ENABLED": False,
    "MIG_REVIEW": False,
    "MIG_REPAIR": False,
    "MIG_GENERATE": False,
    "MIG_PREPARATION": False,
    "MIG_INCREMENTAL": False,
    "MIG_CHAT": False,
    "MIG_SUMMARY": False,
    "MIG_NARRATIVE": False,
}

LANGGRAPH_TASK_SEMANTIC_FLAGS = dict(LEGACY_TASK_SEMANTIC_FLAGS)

SCHEMA_VERSION = "ce05.task-freeze.v1"

# 23 个全 false flags 的 canonical JSON SHA-256（UTF-8 / key 排序 / 紧凑）
_FIXED_DIGEST = "330413c92d4a7273e00cad6c7d5ecea3b4dbc4610c4c48304719ac32d7dc8b26"


def _canonical_json(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_flags_digest(task_semantic_flags: dict) -> str:
    """由 task_semantic_flags 计算 canonical SHA-256（与固定常量一致）。"""
    return hashlib.sha256(_canonical_json(task_semantic_flags)).hexdigest()


def build_compatibility_profile(*, engine: str, reason: str, engine_version: str) -> dict:
    """构造固定 compatibility profile 对象。

    身份由 engine / compatibility_profile / context_engine_version /
    engine_decision_reason 区分；flags_digest 两套相同（flags 相同）。
    """
    flags = LANGGRAPH_TASK_SEMANTIC_FLAGS if engine == "langgraph" else LEGACY_TASK_SEMANTIC_FLAGS
    return {
        "schema_version": SCHEMA_VERSION,
        "compatibility_profile": f"ce05.{engine}.v1",
        "engine": engine,
        "engine_decision_reason": reason,
        "canary_bucket": None,
        "workspace_key": None,
        "context_engine_version": engine_version,
        "context_policy_version": "v1",
        "security_policy_version_at_create": "v1",
        "task_semantic_flags": flags,
        "flags_digest": _FIXED_DIGEST,
        "created_at": None,  # 回填时写入
    }


# 固定 Profile 身份常量
LEGACY_PROFILE = build_compatibility_profile(
    engine="legacy", reason="legacy_task_backfill", engine_version="v2"
)
LANGGRAPH_PROFILE = build_compatibility_profile(
    engine="langgraph", reason="langgraph_task_backfill", engine_version="v3"
)

FIXED_FLAGS_DIGEST = _FIXED_DIGEST

# 可冻结的 Task-semantic Flag key 白名单（23 项，无省略）
FROZEN_FLAG_KEYS = frozenset(LEGACY_TASK_SEMANTIC_FLAGS.keys())

_FLAG_ATTRIBUTE_BY_NAME = {
    "CONTEXT_ENGINE_AGENT_ENABLED": "context_engine_agent_enabled",
    "CONTEXT_TOOL_OUTPUT_GOVERNANCE_ENABLED": "context_tool_output_governance_enabled",
    "CONTEXT_MEMORY_READ_ENABLED": "context_memory_read_enabled",
    "CONTEXT_MEMORY_WRITE_ENABLED": "context_memory_write_enabled",
    "CONTEXT_MEMORY_AUTO_EXTRACT_ENABLED": "context_memory_auto_extract_enabled",
    "CONTEXT_MEMORY_AUTO_ACTIVATE_ENABLED": "context_memory_auto_activate_enabled",
    "CONTEXT_RETRIEVAL_ENABLED": "context_retrieval_enabled",
    "CONTEXT_LEXICAL_RETRIEVAL_ENABLED": "context_lexical_retrieval_enabled",
    "CONTEXT_DENSE_RETRIEVAL_ENABLED": "context_dense_retrieval_enabled",
    "CONTEXT_HYBRID_FUSION_ENABLED": "context_hybrid_fusion_enabled",
    "CONTEXT_RERANK_ENABLED": "context_rerank_enabled",
    "CONTEXT_COMPACTION_ENABLED": "context_compaction_enabled",
    "CONTEXT_CONVERSATION_COMPACTION_ENABLED": "context_conversation_compaction_enabled",
    "CONTEXT_AGENT_LOOP_COMPACTION_ENABLED": "context_agent_loop_compaction_enabled",
    "CONTEXT_FULL_REPLACE_ENABLED": "context_full_replace_enabled",
    "MIG_REVIEW": "mig_review",
    "MIG_REPAIR": "mig_repair",
    "MIG_GENERATE": "mig_generate",
    "MIG_PREPARATION": "mig_preparation",
    "MIG_INCREMENTAL": "mig_incremental",
    "MIG_CHAT": "mig_chat",
    "MIG_SUMMARY": "mig_summary",
    "MIG_NARRATIVE": "mig_narrative",
}


def snapshot_task_semantic_flags(flags: object) -> dict[str, bool]:
    """Return the exact 23 task-scoped values effective at task creation."""
    if set(_FLAG_ATTRIBUTE_BY_NAME) != set(FROZEN_FLAG_KEYS):
        raise RuntimeError("task semantic flag snapshot mapping is incomplete")
    return {
        name: bool(getattr(flags, attribute, False))
        for name, attribute in _FLAG_ATTRIBUTE_BY_NAME.items()
    }

__all__ = [
    "SCHEMA_VERSION",
    "FIXED_FLAGS_DIGEST",
    "FROZEN_FLAG_KEYS",
    "LEGACY_TASK_SEMANTIC_FLAGS",
    "LANGGRAPH_TASK_SEMANTIC_FLAGS",
    "LEGACY_PROFILE",
    "LANGGRAPH_PROFILE",
    "compute_flags_digest",
    "build_compatibility_profile",
    "snapshot_task_semantic_flags",
]
# auto-appended module-level note: freeze profiles: 已冻结的 profile 集合(灰度期禁止变更)。
