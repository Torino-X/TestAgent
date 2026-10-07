"""Artifact schemas (download)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ArtifactDetail(BaseModel):
    artifact_id: str
    artifact_type: str
    file_name: str
    file_ext: str
    file_size: int | None = None
    file_size_display: str = ""
    status: str
    version_no: int
    created_at: str
# schemas.artifact:Artifact 下载相关 Pydantic 契约(ArtifactDetail / 下载 URL 等);前后端共享。
