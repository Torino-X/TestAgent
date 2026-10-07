"""Immutable canonical template file version."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TemplateVersion(Base):
    __tablename__ = "template_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    template_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("template_assets.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(32), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    cover_status: Mapped[str] = mapped_column(String(32), nullable=False, default="missing", server_default="missing")
    cover_kind: Mapped[str | None] = mapped_column(String(32))
    cover_storage_path: Mapped[str | None] = mapped_column(String(1024))
    cover_size: Mapped[int | None] = mapped_column(BigInteger)
    cover_payload_json: Mapped[dict | None] = mapped_column(JSON)
    cover_error: Mapped[str | None] = mapped_column(Text)
    cover_generated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("template_id", "version_no", name="uq_template_versions_template_version"),
        Index("ix_template_versions_template_created", "template_id", "created_at"),
        Index("ix_template_versions_file_hash", "file_hash"),
        Index("ix_template_versions_cover_status", "cover_status", "created_at"),
    )
