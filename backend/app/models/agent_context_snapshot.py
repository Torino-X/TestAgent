"""AgentContextSnapshot model — persisted checkpoint for AgentContext."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AgentContextSnapshot(Base):
    """Stores serialized AgentContext at key orchestration nodes.

    Small snapshots (≤ 256 KB) are stored inline in ``snapshot_json``.
    Larger ones are gzip-compressed and saved to ``blob_path`` under
    ``data/snapshots/``.  ``blob_path`` is internal and must never be
    exposed via the API.
    """

    __tablename__ = "agent_context_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agent_tasks.id"), nullable=False)
    snapshot_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    node_name: Mapped[str] = mapped_column(String(64), nullable=False)
    resume_node: Mapped[str | None] = mapped_column(String(64))
    storage_mode: Mapped[str] = mapped_column(String(32), nullable=False)  # "inline_json" | "blob_file"
    snapshot_json: Mapped[dict | None] = mapped_column(JSON)
    blob_path: Mapped[str | None] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    checksum: Mapped[str | None] = mapped_column(String(128))  # SHA-256 hex

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
# models.agent_context_snapshot:AgentContext 序列化快照表(关键编排节点持久化);支持跨轮恢复 + 审计回溯。
