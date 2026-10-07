"""Incremental Agent — Artifact Chain Writer (Phase 2.5).

决策 B:无 Alembic,使用 ``task_context_json.superseded_artifact_ids`` 字段
记录整条 artifact 链上被取代的 artifact internal_id。

职责:

1. ``incremental_artifact_idempotency_key(...)``:基于 ``(source_task_id,
   source_artifact_id, incremental_modification_id)`` 计算 SHA16 → ``artifact-inc-<sha16>``;
2. ``create_incremental_artifact_record(...)``:事务中 INSERT 新
   ``Artifact(version_no=source.version_no + 1, source_artifact_id=source.id)``;
3. ``append_superseded_to_task_context(...)``:把 ``source.id`` 追加到
   ``agent_tasks.task_context_json.superseded_artifact_ids``,带 CAS;
4. ``IncrementalArtifactChainResult`` —— Pydantic 模型,供 agent_loop 用。

设计要点:
- 全部幂等:同一 idempotency_key 二次调用 → 返回旧 record,不重复 INSERT;
- CAS 写入 task_context_json 防并发覆盖;
- 不写 SQL event 表(避免和 GraphEventAdapter 重复);事件由
  ``IncrementalEventEmitter`` 走 ``event_sink.emit``。
"""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Public types ─────────────────────────────────────────────────


class IncrementalArtifactChainResult(BaseModel):
    """agent_loop 的返回值,告诉主图写入哪个新 artifact。"""

    model_config = {"extra": "forbid"}

    success: bool
    new_artifact_public_id: Optional[str] = Field(default=None, max_length=64)
    new_artifact_version_no: Optional[int] = Field(default=None, ge=2)
    source_artifact_internal_id: Optional[int] = Field(default=None, ge=1)
    superseded_chain: List[int] = Field(default_factory=list)
    idempotency_key: str = Field(max_length=128)
    fallback_reason: Optional[str] = Field(default=None, max_length=240)


# ── Idempotency key ─────────────────────────────────────────────


def incremental_artifact_idempotency_key(
    *,
    source_task_public_id: str,
    source_artifact_public_id: str,
    source_artifact_version_no: int,
    incremental_modification_id: str,
) -> str:
    """计算稳定的 16-char SHA 幂等 key。

    Returns:
        ``artifact-inc-<16hex>``
    """
    raw = (
        f"{source_task_public_id}|{source_artifact_public_id}|"
        f"{source_artifact_version_no}|{incremental_modification_id}"
    ).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"artifact-inc-{digest}"


# ── Public DB ops ───────────────────────────────────────────────


async def create_incremental_artifact_record(
    *,
    session,
    source_artifact,
    new_public_id: str,
    storage_path: str | None = None,
    idempotency_key: str,
    owner_user_id: int,
    file_name: str | None = None,
    storage_url: str | None = None,
) -> Any:
    """INSERT 新 Artifact row(version_no=source.version_no + 1)。

    Returns:
        新 ``Artifact`` SQLAlchemy 实例
    """
    # 延迟导入避免启动期循环
    from app.models.artifact import Artifact

    resolved_storage_path = (
        storage_path
        or storage_url
        or getattr(source_artifact, "storage_path", None)
    )
    if not resolved_storage_path:
        raise RuntimeError("incremental_artifact_storage_path_missing")

    resolved_file_name = (
        file_name
        or getattr(source_artifact, "file_name", None)
        or "incremental_test_plan.docx"
    )
    file_ext = getattr(source_artifact, "file_ext", None) or ".docx"
    if file_ext and not str(file_ext).startswith("."):
        file_ext = f".{file_ext}"

    new_artifact = Artifact(
        public_id=new_public_id,
        user_id=owner_user_id,
        conversation_id=source_artifact.conversation_id,
        task_id=source_artifact.task_id,
        project_id=getattr(source_artifact, "project_id", None),
        artifact_type=getattr(source_artifact, "artifact_type", None) or "test_plan_word",
        file_name=resolved_file_name,
        file_ext=file_ext,
        mime_type=getattr(source_artifact, "mime_type", None),
        file_size=getattr(source_artifact, "file_size", None),
        file_hash=getattr(source_artifact, "file_hash", None),
        storage_type=getattr(source_artifact, "storage_type", None) or "local",
        storage_path=resolved_storage_path,
        status="available",
        version_no=source_artifact.version_no + 1,
        source_artifact_id=source_artifact.id,
        metadata_json={
            "source_artifact_public_id": getattr(source_artifact, "public_id", None),
            "incremental": True,
        },
        idempotency_key=idempotency_key,
        input_hash=getattr(source_artifact, "input_hash", None),
        graph_run_id=getattr(source_artifact, "graph_run_id", None),
        graph_version=getattr(source_artifact, "graph_version", None),
    )
    session.add(new_artifact)
    await _flush_session(session)
    return new_artifact


def append_superseded_to_task_context(
    *,
    session,
    agent_task,
    superseded_internal_id: int,
    idempotency_key: str,
    new_artifact_internal_id: int,
    expected_context_version: int,
) -> Dict[str, Any]:
    """CAS 写入 ``task_context_json.superseded_artifact_ids``。

    Returns:
        更新后的 ``task_context_json`` dict
    """
    raw = agent_task.task_context_json or {}
    if not isinstance(raw, dict):
        raw = {}

    current_version = int(raw.get("context_version", 0) or 0)
    if current_version != expected_context_version:
        raise RuntimeError(
            f"task_context_cas_mismatch "
            f"(expected={expected_context_version}, current={current_version})"
        )

    chain = list(raw.get("superseded_artifact_ids") or [])
    if superseded_internal_id not in chain:
        chain.append(superseded_internal_id)

    idem_records = list(raw.get("idempotency_records") or [])
    idem_records.append({
        "idempotency_key": idempotency_key,
        "new_artifact_internal_id": new_artifact_internal_id,
        "superseded_internal_id": superseded_internal_id,
    })

    new_ctx = {
        **raw,
        "superseded_artifact_ids": chain,
        "idempotency_records": idem_records,
        "context_version": current_version + 1,
    }

    agent_task.task_context_json = new_ctx
    _flush_sync_session_if_possible(session)
    return new_ctx


def find_idempotent_record(
    *,
    session,
    agent_task,
    idempotency_key: str,
) -> Optional[Dict[str, Any]]:
    """如果已存在同一 idempotency_key,返回之前的 record;否则 None。"""
    raw = agent_task.task_context_json or {}
    if not isinstance(raw, dict):
        return None
    records = raw.get("idempotency_records") or []
    for rec in records:
        if rec.get("idempotency_key") == idempotency_key:
            return rec
    return None


def build_superseded_payload_for_state(
    *,
    source_artifact,
    new_artifact,
    existing_records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """构造 ``IncrementalResult.superseded_artifact_public_ids`` 的输入。

    返回的 dict 包含:
    - chain: 完整 internal_id 链(含本次 + 历史)
    - chain_public_ids: 对应的 public_id 列表(供前端展示)
    """
    chain = list(existing_records) + [source_artifact.id]
    return {
        "chain_internal_ids": chain,
        "new_artifact_public_id": new_artifact.public_id,
        "new_artifact_version_no": new_artifact.version_no,
        "source_artifact_internal_id": source_artifact.id,
    }


# ── Helpers ─────────────────────────────────────────────────────


def derive_locked_section_ids_from_confirm_config(
    section_confirm_config: Optional[Dict[str, Any]],
) -> List[str]:
    """从 ``section_confirm_config.sections[].suggested_action == "keep_template"``
    派生 ``locked_section_ids`` 列表。

    镜像 ``langgraph_run_coordinator._derive_locked_section_ids``。
    """
    if not section_confirm_config:
        return []
    sections = section_confirm_config.get("sections") or []
    locked: List[str] = []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        if sec.get("suggested_action") == "keep_template":
            sid = sec.get("section_id")
            if isinstance(sid, str) and sid:
                locked.append(sid)
    return locked


async def _flush_session(session) -> None:
    flush = getattr(session, "flush", None)
    if not callable(flush):
        return
    result = flush()
    if inspect.isawaitable(result):
        await result


def _flush_sync_session_if_possible(session) -> None:
    flush = getattr(session, "flush", None)
    if not callable(flush):
        return
    if inspect.iscoroutinefunction(flush):
        return
    result = flush()
    if inspect.isawaitable(result):
        close = getattr(result, "close", None)
        if callable(close):
            close()


__all__ = [
    "IncrementalArtifactChainResult",
    "incremental_artifact_idempotency_key",
    "create_incremental_artifact_record",
    "append_superseded_to_task_context",
    "find_idempotent_record",
    "build_superseded_payload_for_state",
    "derive_locked_section_ids_from_confirm_config",
]

# module-level note (auto-appended):
# Artifact 链式版本管理。
# 关键约束: 不覆盖已有产物(只链尾追加 version)。
