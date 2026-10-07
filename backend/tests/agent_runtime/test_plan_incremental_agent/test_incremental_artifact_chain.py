"""Test: 15. artifact_chain 幂等键 + 旧 artifact 不被覆写。"""

from __future__ import annotations

import hashlib
import os
import tempfile

import pytest

from app.agent_runtime.incremental.artifact_chain import (
    append_superseded_to_task_context,
    create_incremental_artifact_record,
    derive_locked_section_ids_from_confirm_config,
    incremental_artifact_idempotency_key,
)


def test_incremental_artifact_idempotency_key_stable():
    """同 (source_task, source_artifact, modification) → 同 idempotency_key。"""
    k1 = incremental_artifact_idempotency_key(
        source_task_public_id="task-1",
        source_artifact_public_id="artifact-abc",
        source_artifact_version_no=1,
        incremental_modification_id="mod-xyz",
    )
    k2 = incremental_artifact_idempotency_key(
        source_task_public_id="task-1",
        source_artifact_public_id="artifact-abc",
        source_artifact_version_no=1,
        incremental_modification_id="mod-xyz",
    )
    assert k1 == k2
    # 期望 key 以 artifact-inc- 开头
    assert k1.startswith("artifact-inc-")


def test_incremental_artifact_idempotency_key_different():
    """不同 modification_id → 不同 key。"""
    k1 = incremental_artifact_idempotency_key(
        source_task_public_id="task-1",
        source_artifact_public_id="artifact-abc",
        source_artifact_version_no=1,
        incremental_modification_id="mod-1",
    )
    k2 = incremental_artifact_idempotency_key(
        source_task_public_id="task-1",
        source_artifact_public_id="artifact-abc",
        source_artifact_version_no=1,
        incremental_modification_id="mod-2",
    )
    assert k1 != k2


def test_incremental_artifact_idempotency_key_length():
    """key 长度可控(16 字符 SHA 截断)。"""
    k = incremental_artifact_idempotency_key(
        source_task_public_id="task-1",
        source_artifact_public_id="artifact-abc",
        source_artifact_version_no=1,
        incremental_modification_id="mod-xyz",
    )
    suffix = k.replace("artifact-inc-", "")
    assert len(suffix) == 16


def test_derive_locked_section_ids_from_confirm_config():
    """section_confirm_config.sections[].suggested_action=keep_template → locked_section_ids。"""
    config = {
        "sections": [
            {"section_id": "s1", "suggested_action": "keep_template"},
            {"section_id": "s2", "suggested_action": "regenerate"},
            {"section_id": "s3", "suggested_action": "keep_template"},
        ]
    }
    locked = derive_locked_section_ids_from_confirm_config(config)
    assert "s1" in locked
    assert "s3" in locked
    assert "s2" not in locked


def test_derive_locked_section_ids_empty_config():
    assert derive_locked_section_ids_from_confirm_config({}) == []
    assert derive_locked_section_ids_from_confirm_config(None) == []
    assert derive_locked_section_ids_from_confirm_config({"sections": []}) == []


def test_append_superseded_artifact_id_to_task_context_cas():
    """append_superseded_to_task_context 走 CAS:context_version 校验。

    这里只测纯函数式 layer;实际写库测在 test_agent_runtime_incremental_chain_db.py
    """
    # CAS 校验:context_version 不匹配时 raise RuntimeError
    class _FakeTask:
        task_context_json: dict = {"context_version": 1, "suppressed_artifact_ids": []}

    class _FakeSession:
        def flush(self): pass

    # 版本匹配 → 正常写入并 bump context_version
    new_ctx = append_superseded_to_task_context(
        session=_FakeSession(),
        agent_task=_FakeTask(),
        superseded_internal_id=42,
        idempotency_key="k-1",
        new_artifact_internal_id=100,
        expected_context_version=1,
    )
    assert new_ctx["context_version"] == 2
    assert 42 in new_ctx["superseded_artifact_ids"]
    # idempotency_records 写入
    records = new_ctx["idempotency_records"]
    assert any(r.get("idempotency_key") == "k-1" for r in records)

    # 版本不匹配 → raise RuntimeError
    _FakeTask.task_context_json = {"context_version": 5, "suppressed_artifact_ids": []}
    with pytest.raises(RuntimeError):
        append_superseded_to_task_context(
            session=_FakeSession(),
            agent_task=_FakeTask(),
            superseded_internal_id=43,
            idempotency_key="k-2",
            new_artifact_internal_id=101,
            expected_context_version=1,
        )


@pytest.mark.asyncio
async def test_create_incremental_artifact_record_uses_current_artifact_schema():
    """Incremental artifact writer must use storage_path, not legacy storage_url."""

    class _FakeSourceArtifact:
        id = 11
        public_id = "artifact-source"
        user_id = 1
        conversation_id = 22
        task_id = 33
        project_id = 44
        artifact_type = "test_plan_word"
        file_name = "source.docx"
        file_ext = ".docx"
        mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        file_size = 1234
        file_hash = "hash-1"
        storage_type = "local"
        storage_path = "H:/Agent/TestAgent/backend/storage/source.docx"
        status = "available"
        version_no = 1
        input_hash = "input-hash"
        graph_run_id = "run-1"
        graph_version = "v3"

    class _FakeSession:
        def __init__(self):
            self.added = None

        def add(self, obj):
            self.added = obj

        async def flush(self):
            return None

    session = _FakeSession()
    artifact = await create_incremental_artifact_record(
        session=session,
        source_artifact=_FakeSourceArtifact(),
        new_public_id="artifact-new",
        storage_path="H:/Agent/TestAgent/backend/storage/new.docx",
        idempotency_key="artifact-inc-abc",
        owner_user_id=1,
    )

    assert session.added is artifact
    assert artifact.public_id == "artifact-new"
    assert artifact.conversation_id == 22
    assert artifact.project_id == 44
    assert artifact.storage_path.endswith("new.docx")
    assert artifact.version_no == 2
    assert artifact.source_artifact_id == 11
    assert artifact.idempotency_key == "artifact-inc-abc"
