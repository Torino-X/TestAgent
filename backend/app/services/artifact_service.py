"""Artifact service — list, detail, and download generated artifacts.

Phase 2.9A.21 §7.1 跨用户/任务权限加固:
  * get_download_stream:校验 artifact 归属用户 + 文件存在 + MIME + 文件名安全
  * 越权直接抛 NotFound,不暴露存在性
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.agent_task_repository import AgentTaskRepository
from app.repositories.artifact_repository import ArtifactRepository
from app.storage.oss_storage import object_storage
from app.utils.file_utils import readable_size


# Phase 2.9A.21:文件名安全 — 不允许路径穿越字符 / 绝对路径 / 控制字符
_FILENAME_SAFE_RE = re.compile(r"^[A-Za-z0-9_.\- ()（）一-鿿]+$")


def _is_safe_filename(name: str) -> bool:
    """校验文件名安全性 — 仅允许中英文字符、数字、有限 punctuation。"""
    if not name or len(name) > 255:
        return False
    # 拒绝路径穿越 / 隐藏文件 / 控制字符
    if name.startswith((".", "/", "\\")) or "/" in name or "\\" in name:
        return False
    if not _FILENAME_SAFE_RE.match(name):
        return False
    return True


class ArtifactService:
    """Manages generated artifacts — real DB reads and file streaming."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._art_repo = ArtifactRepository(session)
        self._task_repo = AgentTaskRepository(session)

    async def list_artifacts(
        self, task_public_id: str, user_internal_id: int
    ) -> tuple[list[dict], int]:
        # Phase 2.9A.21 §7.1:严格校验 task 归属
        task = await self._task_repo.get_by_public_id(task_public_id)
        if not task or task.user_id != user_internal_id:
            return [], 0
        artifacts = await self._art_repo.list_by_task(user_internal_id, task.id)
        return [self._to_summary(a) for a in artifacts], len(artifacts)

    async def get_artifact(
        self, artifact_public_id: str, user_internal_id: int
    ) -> dict:
        artifact = await self._art_repo.get_by_public_id(artifact_public_id)
        # Phase 2.9A.21 §7.1:严格 user_id 隔离,越权直接 NotFound
        if not artifact or artifact.user_id != user_internal_id:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("产物")
        return self._to_detail(artifact)

    async def get_download_stream(
        self, artifact_public_id: str, user_internal_id: int
    ) -> tuple:
        """Phase 2.9A.21 §7.1 下载能力 + 越权拒绝 + 文件存在校验 + MIME + 文件名安全。

        强约束:
          * artifact.user_id 必须等于 user_internal_id;
          * artifact.storage_path 指向物理文件必须存在;
          * artifact.file_name 必须通过 _is_safe_filename;
          * MIME 与 file_size 优先读 DB 列,文件读取只能 stream,绝不打包绝对路径。
        """
        artifact = await self._art_repo.get_by_public_id(artifact_public_id)
        if not artifact or artifact.user_id != user_internal_id:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("产物")

        storage_path = artifact.storage_path
        if not storage_path:
            from app.core.exceptions import ArtifactStorageError
            raise ArtifactStorageError(
                detail={"reason": "storage_path_empty", "artifact_id": artifact_public_id}
            )

        # Phase 2.9A.21:物理文件必须存在
        if not await object_storage.exists(storage_path):
            from app.core.exceptions import ArtifactStorageError
            raise ArtifactStorageError(
                detail={"reason": "file_missing", "artifact_id": artifact_public_id}
            )

        # 文件名安全
        if not _is_safe_filename(artifact.file_name or ""):
            from app.core.exceptions import ArtifactStorageError
            raise ArtifactStorageError(
                detail={
                    "reason": "unsafe_filename",
                    "artifact_id": artifact_public_id,
                }
            )

        # Capture needed fields before session closes (StreamingResponse
        # lazily evaluates the generator, by which time the DI session
        # may already be committed/closed).
        _storage_path = storage_path
        _file_name = artifact.file_name
        _mime_type = artifact.mime_type or "application/octet-stream"

        async def stream():
            async for chunk in object_storage.open_file(_storage_path):
                yield chunk

        return stream, _file_name, _mime_type

    @staticmethod
    def _to_summary(a) -> dict:
        return {
            "artifact_id": a.public_id,
            "artifact_type": a.artifact_type,
            "file_name": a.file_name,
            "file_ext": a.file_ext,
            "file_size": a.file_size,
            "status": a.status,
            "version_no": a.version_no,
            "created_at": a.created_at.isoformat() if a.created_at else "",
        }

    @staticmethod
    def _to_detail(a) -> dict:
        return {
            "artifact_id": a.public_id,
            "artifact_type": a.artifact_type,
            "file_name": a.file_name,
            "file_ext": a.file_ext,
            "file_size": a.file_size,
            "file_size_display": readable_size(a.file_size or 0),
            "status": a.status,
            "version_no": a.version_no,
            "mime_type": a.mime_type,
            "storage_path_present": bool(a.storage_path),
            "created_at": a.created_at.isoformat() if a.created_at else "",
        }


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (Artifact 下载 / 查询):
#
#   链路:
#     前端 chat → 点击"下载产物"
#       → api/v1/artifacts.py GET /artifacts/{public_id}
#         → ArtifactService.get_for_download(public_id, user_id)
#           → 校验 owner (user_id + 对话权限)
#           → 读 storage_path → local_storage.open_bytes
#           → 流式返回(StreamingResponse in routers)
#
# 关键约束(供开发者速查):
#   - 仅返回 user 拥有的 artifact(强校验,否则 403);
#   - 大文件走 StreamingResponse,避免一次性 load;
#   - 删除 artifact 由本服务 + 本地存储 + DB 同步执行(分布式事务简化)。
