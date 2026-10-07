"""Canonical template entity exposed by the template marketplace."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TemplateAsset(Base):
    __tablename__ = "template_assets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category_code: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    tags_json: Mapped[list[str] | None] = mapped_column(JSON)
    visibility: Mapped[str] = mapped_column(String(32), default="private", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    current_version_id: Mapped[int | None] = mapped_column(BigInteger)
    save_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_template_assets_owner_deleted", "owner_user_id", "deleted_at"),
        Index("ix_template_assets_market_updated", "visibility", "status", "deleted_at", "updated_at"),
        Index("ix_template_assets_category_market", "category_code", "visibility", "status"),
        Index("ix_template_assets_updated", "updated_at"),
    )
