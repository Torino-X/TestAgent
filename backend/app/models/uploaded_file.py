"""UploadedFile model — file metadata (not binary)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("conversations.id"), nullable=True)
    task_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agent_tasks.id"))
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(32), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_hash: Mapped[str | None] = mapped_column(String(128))
    file_type: Mapped[str] = mapped_column(String(64), default="unknown")
    upload_status: Mapped[str] = mapped_column(String(32), default="uploaded")
    storage_type: Mapped[str] = mapped_column(String(32), default="local")
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    conversation = relationship("Conversation", back_populates="files")

    __table_args__ = (
        Index(
            "ix_uploaded_files_library_active_order",
            "user_id",
            "deleted_at",
            "updated_at",
            "id",
        ),
        Index(
            "ix_uploaded_files_library_deleted_order",
            "user_id",
            "deleted_at",
            "id",
        ),
    )


# 模块定位:UploadedFile 模型(用户上传文件元数据)
#
# 字段:
#   - id / public_id / user_internal_id / conversation_id
#   - file_name / file_size / mime_type
#   - file_type(docx / pdf / md / txt / image / ...)
#   - capability_identified(bool,FileUnderstandingService 写入)
#   - upload_status(queued / completed / failed)
#   - storage_path
#
# 链路:
#   FileService.save_upload → local_storage.save_bytes + 上传行
#   FileService.confirm_type → update file_type
#   FileUnderstandingService → 异步 → update file_type + capability_identified
#
# 关键约束:
#   - file_type 在 confirm 前为 None(后端未识别);
#   - storage_path **不**对外暴露;
#   - file_size 上限 user 配置(默认 50MB);
#   - 删除要清 storage_path(否则垃圾残留)。
