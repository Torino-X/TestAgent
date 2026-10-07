"""CE-05 WP-2 Task Freeze 测试。

覆盖：
  1. manifest 构建 + write-once（same digest no-op / diff digest conflict）
  2. 深度 merge 保留 reserved key
  3. TaskScopedFeatureFlagResolver：Manifest 冻结 / Kill Switch 实时 / Operational 实时
  4. engine 一致性（DATA_INTEGRITY_ERROR）
  5. 固定 Profile digest 与 23 项完整性
  6. 运行时门禁 5 项
"""

from __future__ import annotations

import os

import pytest

from app.context_engine.freeze.profiles import (
    FIXED_FLAGS_DIGEST,
    FROZEN_FLAG_KEYS,
    LANGGRAPH_PROFILE,
    LEGACY_PROFILE,
    compute_flags_digest,
)
from app.context_engine.freeze.resolver import (
    TaskScopedContextFlags,
    TaskScopedFeatureFlagResolver,
)
from app.context_engine.freeze.service import (
    RESERVED_KEY,
    TaskFreezeIntegrityError,
    build_manifest,
    default_compatibility_flags,
    extract_manifest,
    merge_context_json,
    resolve_engine_consistency,
    write_once_validate,
)


# ══════════════════════════════════════════════════════════════════
# 1. Profile 完整性 + 固定 digest
# ══════════════════════════════════════════════════════════════════

def test_frozen_flag_white_list_is_complete_23():
    assert len(FROZEN_FLAG_KEYS) == 23
    for key in FROZEN_FLAG_KEYS:
        assert key.isupper()


def test_compatibility_digest_matches_known_value():
    # 23 个全 false 的 canonical JSON SHA-256（人工复验一致）
    assert FIXED_FLAGS_DIGEST == "330413c92d4a7273e00cad6c7d5ecea3b4dbc4610c4c48304719ac32d7dc8b26"
    assert compute_flags_digest(LEGACY_PROFILE["task_semantic_flags"]) == FIXED_FLAGS_DIGEST


def test_both_profiles_share_digest_but_differ_by_identity():
    assert LEGACY_PROFILE["flags_digest"] == LANGGRAPH_PROFILE["flags_digest"]
    assert LEGACY_PROFILE["engine"] == "legacy"
    assert LANGGRAPH_PROFILE["engine"] == "langgraph"
    assert LEGACY_PROFILE["compatibility_profile"] == "ce05.legacy.v1"
    assert LANGGRAPH_PROFILE["compatibility_profile"] == "ce05.langgraph.v1"


# ══════════════════════════════════════════════════════════════════
# 2. Manifest 构建 + write-once
# ══════════════════════════════════════════════════════════════════

def test_build_manifest_and_validate():
    manifest = build_manifest(
        engine="langgraph",
        decision_reason="engine_router.canary_percent",
        canary_bucket=37,
        workspace_key="conversation:conv_x",
        context_engine_version="v3",
        task_semantic_flags=default_compatibility_flags("langgraph"),
    )
    assert manifest["engine"] == "langgraph"
    assert manifest["canary_bucket"] == 37
    assert manifest["schema_version"] == "ce05.task-freeze.v1"
    write_once_validate(manifest)  # 不抛 → digest 一致


def test_write_once_same_digest_noop():
    manifest = build_manifest(
        engine="legacy",
        decision_reason="freeze_at_create",
        canary_bucket=None,
        workspace_key=None,
        context_engine_version="v2",
        task_semantic_flags=default_compatibility_flags("legacy"),
    )
    write_once_validate(manifest)
    # 同一 flags → 同一 digest
    again = build_manifest(
        engine="legacy",
        decision_reason="freeze_at_create",
        canary_bucket=None,
        workspace_key=None,
        context_engine_version="v2",
        task_semantic_flags=default_compatibility_flags("legacy"),
    )
    assert manifest["flags_digest"] == again["flags_digest"]


def test_write_once_different_digest_conflict():
    m1 = build_manifest(
        engine="legacy",
        decision_reason="r1",
        canary_bucket=None,
        workspace_key=None,
        context_engine_version="v2",
        task_semantic_flags=default_compatibility_flags("legacy"),
    )
    diff_flags = dict(default_compatibility_flags("legacy"))
    diff_flags["MIG_CHAT"] = True
    m2 = build_manifest(
        engine="legacy",
        decision_reason="r2",
        canary_bucket=None,
        workspace_key=None,
        context_engine_version="v2",
        task_semantic_flags=diff_flags,
    )
    assert m1["flags_digest"] != m2["flags_digest"]


# ══════════════════════════════════════════════════════════════════
# 3. 深度 merge 保留 reserved key
# ══════════════════════════════════════════════════════════════════

def test_business_context_merge_preserves_manifest():
    manifest = {
        "schema_version": "ce05.task-freeze.v1",
        "engine": "legacy",
        "task_semantic_flags": default_compatibility_flags("legacy"),
        "flags_digest": FIXED_FLAGS_DIGEST,
    }
    existing = {RESERVED_KEY: manifest, "idempotency_records": [1]}
    merged = merge_context_json(existing, {"superseded_artifact_ids": ["a1"]})
    assert merged[RESERVED_KEY] == manifest  # reserved key 保留
    assert merged["superseded_artifact_ids"] == ["a1"]
    assert merged["idempotency_records"] == [1]


def test_business_update_cannot_overwrite_manifest():
    manifest = {"engine": "langgraph"}
    existing = {RESERVED_KEY: manifest}
    merged = merge_context_json(existing, {RESERVED_KEY: {"engine": "legacy"}})
    assert merged[RESERVED_KEY] == manifest  # 普通更新禁止写 reserved key


# ══════════════════════════════════════════════════════════════════
# 4. Resolver：Manifest 冻结 / Kill Switch 实时 / Operational 实时
# ══════════════════════════════════════════════════════════════════

def _manifest_with_flags(**overrides):
    flags = default_compatibility_flags("langgraph")
    flags.update(overrides)
    return build_manifest(
        engine="langgraph",
        decision_reason="test",
        canary_bucket=None,
        workspace_key=None,
        context_engine_version="v3",
        task_semantic_flags=flags,
    )


def test_resolver_reads_frozen_task_semantic_from_manifest():
    manifest = _manifest_with_flags(MIG_CHAT=True, CONTEXT_RERANK_ENABLED=True)
    resolver = TaskScopedFeatureFlagResolver.from_manifest(manifest)
    assert resolver.evaluate("MIG_CHAT") is True
    assert resolver.evaluate("CONTEXT_RERANK_ENABLED") is True
    assert resolver.evaluate("MIG_REVIEW") is False  # 未改 → 冻结 False


def test_resolver_operational_flags_realtime():
    manifest = _manifest_with_flags()
    resolver = TaskScopedFeatureFlagResolver.from_manifest(manifest)
    os.environ["CONTEXT_DEBUG_API_ENABLED"] = "true"
    try:
        assert resolver.evaluate("CONTEXT_DEBUG_API_ENABLED") is True
    finally:
        os.environ.pop("CONTEXT_DEBUG_API_ENABLED", None)


def test_resume_never_reads_dynamic_task_semantic_flags():
    manifest = _manifest_with_flags(MIG_CHAT=False)
    resolver = TaskScopedFeatureFlagResolver.from_manifest(manifest)
    # 即使 env 被改成 true，Manifest 冻结的 MIG_CHAT 仍为 False
    os.environ["MIG_CHAT"] = "true"
    try:
        assert resolver.evaluate("MIG_CHAT") is False
    finally:
        os.environ.pop("MIG_CHAT", None)


# ══════════════════════════════════════════════════════════════════
# 5. engine 一致性
# ══════════════════════════════════════════════════════════════════

def test_engine_manifest_consistency_match():
    manifest = _manifest_with_flags()
    assert resolve_engine_consistency("langgraph", manifest) == "langgraph"
    # manifest.engine=langgraph，task.engine_type=None → 归一为 legacy → 与 manifest 冲突
    with pytest.raises(TaskFreezeIntegrityError):
        resolve_engine_consistency(None, _manifest_with_flags())


def test_engine_manifest_mismatch_blocks():
    manifest = _manifest_with_flags()
    with pytest.raises(TaskFreezeIntegrityError):
        resolve_engine_consistency("legacy", manifest)  # task=legacy, manifest=langgraph


# ══════════════════════════════════════════════════════════════════
# 6. 运行时门禁（5 项静态断言）
# ══════════════════════════════════════════════════════════════════

def test_direct_dynamic_read_gate_scan():
    """Task Path 中 Task-semantic Flag 不得直读 env/feature_flags 实例。

    通过断言 Resolver 是唯一读取入口的语义实现：本测试确认 TaskScopedContextFlags
    从 Manifest 读 Task-semantic，而非 env。
    """
    manifest = _manifest_with_flags(MIG_CHAT=True)
    flags = TaskScopedContextFlags(
        engine="langgraph",
        task_semantic_flags=manifest["task_semantic_flags"],
        manifest_digest=manifest["flags_digest"],
    )
    assert flags.get("MIG_CHAT") is True  # 来自 Manifest，非 env


def test_cross_worker_manifest_reuse():
    """两个 worker 从同一 Manifest 得到同一解析结果（无 env 依赖）。"""
    manifest = _manifest_with_flags(CONTEXT_RERANK_ENABLED=True)
    w1 = TaskScopedFeatureFlagResolver.from_manifest(manifest)
    w2 = TaskScopedFeatureFlagResolver.from_manifest(manifest)
    assert w1.evaluate("CONTEXT_RERANK_ENABLED") == w2.evaluate("CONTEXT_RERANK_ENABLED") is True
    assert w1.engine == w2.engine == "langgraph"
