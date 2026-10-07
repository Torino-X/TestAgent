"""File schemas."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class FileUploadResponse(BaseModel):
    file_id: str
    file_name: str
    file_size: int
    file_size_display: str
    file_ext: str
    file_type: str
    upload_status: str


class FileConfirmRequest(BaseModel):
    file_type: str = Field(..., description="Desired file type: requirement_doc|test_plan_template|supplemental_doc")


class FileSummary(BaseModel):
    id: str = Field(alias="public_id")
    original_name: str
    file_ext: str
    file_size: int
    file_type: str
    upload_status: str

    model_config = {"from_attributes": True, "populate_by_name": True}


class FileListResponse(BaseModel):
    files: list[FileSummary]
    total: int
# schemas.file:文件上传/下载响应 Pydantic 契约(FileUploadResponse 等);与 file_service 配套。
