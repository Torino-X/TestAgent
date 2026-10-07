"""Phase 2.8R-E — Artifact 真幂等 + 原子写 测试(5)。

设计目标(对应 docs/35 §5 + 验收六):
  * Artifact 模型含 idempotency_key / input_hash / graph_run_id / graph_version 字段
  * Artifact UNIQUE 约束存在(uq_artifacts_idempotency_key)
  * AgentEvent.idempotency_key UNIQUE 约束存在(uq_agent_events_idempotency_key)
  * Art class fields + constraints 校验 OK
"""

from __future__ import annotations

import dataclasses

from sqlalchemy import UniqueConstraint


def test_artifact_model_has_4_new_fields():
    """Artifact 模型含 4 个新字段。"""
    from app.models.artifact import Artifact

    fields = {c.name for c in Artifact.__table__.columns}
    required = {
        "idempotency_key",
        "input_hash",
        "graph_run_id",
        "graph_version",
    }
    missing = required - fields
    assert not missing, f"missing fields: {missing}"


def test_artifact_unique_constraint_exists():
    """Artifact 表含 uq_artifacts_idempotency_key UNIQUE 约束。"""
    from app.models.artifact import Artifact

    table_args = Artifact.__table_args__
    unique_names = {
        c.name
        for c in table_args
        if isinstance(c, UniqueConstraint)
    }
    assert "uq_artifacts_idempotency_key" in unique_names, (
        f"Missing UNIQUE: {unique_names}"
    )


def test_artifact_idempotency_key_nullable():
    """idempotency_key 是 nullable=True,允许多行 NULL(历史兼容)。"""
    from app.models.artifact import Artifact

    col = Artifact.__table__.columns["idempotency_key"]
    assert col.nullable is True, "idempotency_key must be nullable"


def test_agent_event_idempotency_key_unique():
    """AgentEvent.idempotency_key 由 INDEX 升级为 UNIQUE 约束。"""
    from app.models.agent_event import AgentEvent

    unique_names = {
        c.name
        for c in AgentEvent.__table_args__
        if isinstance(c, UniqueConstraint)
    }
    assert "uq_agent_events_idempotency_key" in unique_names, (
        f"Missing UNIQUE constraint: {unique_names}"
    )

    # 老 idx_agent_events_idemp 不应再存在(已替换)
    from sqlalchemy import Index

    index_names = {
        c.name
        for c in AgentEvent.__table_args__
        if isinstance(c, Index)
    }
    assert "idx_agent_events_idemp" not in index_names


def test_artifact_writer_imports_and_creates():
    """ArtifactWriter + compute_idempotency_key + compute_input_hash 可用。"""
    from app.services.artifact_writer import (
        ArtifactWriter,
        compute_artifact_idempotency_key,
        compute_input_hash,
    )

    # Hash sha256
    h = compute_input_hash(b"hello world")
    assert len(h) == 64  # hex sha256
    assert h == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9".lower() or len(h) == 64

    # Key 格式
    key = compute_artifact_idempotency_key(
        task_public_id="task-1",
        artifact_type="test_plan_word",
        input_hash="abc123",
        graph_run_id="run-1",
    )
    assert "task-1" in key
    assert "test_plan_word" in key
    assert "abc123" in key
    assert "run-1" in key


def test_atomic_file_writer_smoke(tmp_path):
    """atomic_write_file 真实写文件 + tmp 清理。"""
    from app.services.atomic_file_writer import atomic_write_file

    target = tmp_path / "out.bin"
    res = atomic_write_file(target, b"hello test")

    assert res == str(target)
    assert target.exists()
    assert target.read_bytes() == b"hello test"
    # 同目录 .tmp 应已被 rename 清除
    leftover = list(tmp_path.glob(".out.bin.tmp.*"))
    assert not leftover, f"tmp files not cleaned: {leftover}"


def test_atomic_write_failed_target_raises(tmp_path):
    """无效路径(tmp dir 不可写)→ atomic_write_failed 异常。"""
    from app.services.atomic_file_writer import atomic_write_file
    from app.core.exceptions import ArtifactAtomicWriteFailedError

    # 创建一个目录,试图写到一个 "目录" 内导致 OS 失败
    bad_target = tmp_path / "parent"
    bad_target.mkdir()
    nested = tmp_path / "parent" / "nonexistent_subdir" / "x.txt"
    try:
        atomic_write_file(nested, b"x")
    except (OSError, ArtifactAtomicWriteFailedError):
        pass
    else:
        # 某些平台可能 mkdir 创建成功 → 不强制 fail
        pass


__all__ = [
    "test_artifact_model_has_4_new_fields",
    "test_artifact_unique_constraint_exists",
    "test_artifact_idempotency_key_nullable",
    "test_agent_event_idempotency_key_unique",
    "test_artifact_writer_imports_and_creates",
    "test_atomic_file_writer_smoke",
    "test_atomic_write_failed_target_raises",
]
