"""Artifact writing orchestrator — Phase 2.8R-E.

把"文件落地 + DB 记录"两个动作合并成一个**幂等**操作:
  1. 写产物到磁盘(临时 + atomic rename)— atomic_file_writer
  2. 创建 Artifact DB 行,带 idempotency_key UNIQUE(create_or_get_by_idempotency_key)
  3. 双进程并发 → 1 行成功 + 1 行 return existing(已 OK,不覆盖)

不在范围:delete / 重新导出 / overwrite。这些 Phase 2.8R-E 后续阶段处理。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.artifact import Artifact
from app.models.agent_task import AgentTask
from app.repositories.artifact_repository import ArtifactRepository
from app.storage.oss_storage import object_storage
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)


async def resolve_task_project_id(session: object, task_internal_id: int) -> int | None:
    """Resolve immutable task ownership only when a real DB session is available.

    Tool-unit contexts intentionally use ``None`` or plain mocks for sessions;
    those represent standalone tasks and must keep the historical no-DB path.
    """
    if not isinstance(session, AsyncSession):
        return None
    return (await session.execute(
        select(AgentTask.project_id).where(AgentTask.id == task_internal_id)
    )).scalar_one_or_none()


def compute_artifact_idempotency_key(
    *,
    task_public_id: str,
    artifact_type: str,
    input_hash: str,
    graph_run_id: Optional[str] = None,
) -> str:
    """生成 Artifact idempotency_key。

    格式:f"{task_public_id}|{artifact_type}|{input_hash}|{graph_run_id or ''}"
    """
    parts = [task_public_id or "", artifact_type or "", input_hash or ""]
    parts.append(graph_run_id or "")
    return "|".join(parts)


def compute_input_hash(content: bytes | str) -> str:
    """input_hash: 内容 sha256 hex(64 chars)。"""
    if isinstance(content, str):
        data = content.encode("utf-8")
    else:
        data = content
    return hashlib.sha256(data).hexdigest()


class ArtifactWriter:
    """Phase 2.8R-E: 落地 artifact 文件 + 幂等 DB 写入。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        file_storage_base: object | None = None,
    ) -> None:
        self._session = session
        self._art_repo = ArtifactRepository(session)
        # Kept as an optional compatibility argument for existing callers.

    async def write_and_record(
        self,
        *,
        user_internal_id: int,
        conversation_internal_id: int,
        task_internal_id: int,
        task_public_id: str,
        artifact_type: str,
        file_name: str,
        file_ext: str,
        mime_type: str | None,
        file_content: bytes,
        graph_run_id: Optional[str] = None,
        graph_version: Optional[str] = None,
        metadata: dict | None = None,
    ) -> tuple[Artifact, bool]:
        """写文件 + 写 DB 行(幂等)。

        Returns:
            (Artifact, created) — created=False 表示已存在(并发情况)
        """
        # 1) input_hash → idempotency_key
        input_hash = compute_input_hash(file_content)
        idempotency_key = compute_artifact_idempotency_key(
            task_public_id=task_public_id,
            artifact_type=artifact_type,
            input_hash=input_hash,
            graph_run_id=graph_run_id,
        )

        # 2) 提前 lookup — 并发情况下拿到现有行的存储路径(可能
        #    已被另一进程 atomic_rename 写完)。如果命中,**直接返回**。
        existing = await self._art_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            # DB 行已存在(并发),不重复写文件 / 不覆盖
            logger.info(
                "ArtifactWriter: idempotent hit, skip write | id=%d public=%s",
                existing.id, existing.public_id,
            )
            return existing, False

        # 3) Persist the completed bytes directly to OSS before recording DB metadata.
        stored = await object_storage.save_artifact(
            file_content, file_name, str(user_internal_id), str(task_internal_id)
        )

        # 4) 写 DB(捕获 UNIQUE 冲突 → 重新 lookup existing)
        file_size = stored.file_size
        now = utcnow()
        project_id = await resolve_task_project_id(self._session, task_internal_id)
        artifact = Artifact(
            public_id=generate_public_id("artifact"),
            user_id=user_internal_id,
            conversation_id=conversation_internal_id,
            task_id=task_internal_id,
            project_id=project_id,
            artifact_type=artifact_type,
            file_name=file_name,
            file_ext=file_ext.lstrip("."),
            mime_type=mime_type,
            file_size=file_size,
            file_hash=None,
            storage_type=object_storage.storage_type,
            storage_path=stored.storage_path,
            status="available",
            version_no=1,
            source_artifact_id=None,
            metadata_json=metadata,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            graph_run_id=graph_run_id,
            graph_version=graph_version,
            created_at=now,
            updated_at=now,
        )
        row, created = await self._art_repo.create_or_get_by_idempotency_key(
            artifact,
            idempotency_key=idempotency_key,
        )
        if not created:
            try:
                await object_storage.delete_file(stored.storage_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ArtifactWriter: duplicate artifact cleanup failed | path=%s | err=%s",
                    stored.storage_path,
                    str(exc)[:200],
                )
        return row, created


__all__ = [
    "ArtifactWriter",
    "compute_artifact_idempotency_key",
    "compute_input_hash",
]


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (Artifact 写库的薄封装):
#
#   链路:
#     WordExportTool.run(...) 成功导出 .docx 后:
#       → ArtifactWriter.write(public_id, file_name, storage_path, size_bytes,
#                              user_id, conversation_id, task_id)
#         → artifact_repository.create(...)
#         → meta(public_id):标识前端下载 URL
#
# 关键约束(供开发者速查):
#   - 本服务只负责写库,不直接写文件;
#   - storage_path 必须是绝对路径;不存在 → raise ArtifactStorageMissing;
#   - 写库必须在文件实际落地后才发生(AtomicFileWriter 保证);
#   - 同一 public_id 二次 write → 视为版本升级(version_no +1)。
