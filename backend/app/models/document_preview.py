"""Durable, owner-scoped derivative metadata for document previews."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DocumentPreview(Base):
    """Tracks the private PDF derivative for one visible library item.

    ``item_public_id`` is globally unique across Library's ``file_`` and
    ``art_`` records. ``source_version`` is a content/storage fingerprint so a
    rename never invalidates a useful PDF, while a replaced source cannot reuse
    an obsolete derivative.
    """

    __tablename__ = "document_previews"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    source_version: Mapped[str] = mapped_column(String(191), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    preview_storage_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    preview_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_document_previews_user_status_updated", "user_id", "status", "updated_at"),
    )
