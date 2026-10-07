"""Owner-scoped template library relation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserTemplate(Base):
    __tablename__ = "user_templates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    template_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("template_assets.id"), nullable=False)
    version_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("template_versions.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("user_id", "template_id", name="uq_user_templates_user_template"),
        Index("ix_user_templates_user_deleted_updated", "user_id", "deleted_at", "updated_at"),
        Index("ix_user_templates_user_source", "user_id", "source_type"),
    )
