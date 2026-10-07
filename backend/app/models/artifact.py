"""Artifact model — generated artifact metadata (Word, etc.).

Phase 2.8R-E: 真幂等性字段 (idempotency_key UNIQUE + input_hash + graph_run_id)。
双进程并发导出会触发 UNIQUE 约束冲突,由调用方负责重新 lookup 已有行。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id"), nullable=False
    )
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent_tasks.id"), nullable=False
    )
    project_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("projects.id"), nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(32), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    file_hash: Mapped[str | None] = mapped_column(String(128))
    storage_type: Mapped[str] = mapped_column(String(32), default="local")
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="available")
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    source_artifact_id: Mapped[int | None] = mapped_column(BigInteger)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    # Phase 2.8R-E: 幂等性必填字段
    # idempotency_key: hash(task_id|artifact_type|input_hash|graph_run_id);
    # 双进程并发写同一内容时只一行成功,其余抛 ArtifactIdempotencyConflictError
    idempotency_key: Mapped[str | None] = mapped_column(String(160), index=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    graph_run_id: Mapped[str | None] = mapped_column(String(64))
    graph_version: Mapped[str | None] = mapped_column(String(32))

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    task = relationship("AgentTask", back_populates="artifacts")

    __table_args__ = (
        Index(
            "ix_artifacts_library_active_order",
            "user_id",
            "status",
            "deleted_at",
            "updated_at",
            "id",
        ),
        Index(
            "ix_artifacts_library_deleted_order",
            "user_id",
            "deleted_at",
            "id",
        ),
        Index(
            "ix_artifacts_user_project_created",
            "user_id",
            "project_id",
            "created_at",
        ),
        # Phase 2.8R-E: 真 UNIQUE 约束(idempotency)
        # NULL 允许,避免历史数据迁移时冲突
        UniqueConstraint(
            "idempotency_key",
            name="uq_artifacts_idempotency_key",
        ),
    )


# 模块定位:Artifact 模型(测试方案产物元数据:Word 等)
#
# 字段:
#   - id / public_id / task_id / user_internal_id / conversation_id
#   - file_name / file_size / mime_type
#   - storage_path(本地绝对路径;不暴露给前端)
#   - version_no(单调递增;同 task 多版产物)
#   - idempotency_key **UNIQUE**(Phase 2.8R-E)
#   - input_hash / graph_run_id
#
# 链路:
#   WordExportTool 成功 → ArtifactRepository.write → atomic 落盘
#   api/v1/artifacts download → 读 storage_path + 强制 owner ACL
#
# 关键约束:
#   - storage_path **绝不**进 API 响应(只后端持有);
#   - idempotency_key UNIQUE:双进程并发导出会触发冲突,调用方重新 lookup;
#   - 同 task_id 多版用 version_no 区分;
#   - 删除要走 ArtifactRepository.delete + 同步删 storage 文件。
