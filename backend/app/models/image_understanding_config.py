"""F020 — image_understanding_configs model.

Per-user configuration for the dedicated image-understanding (vision)
LLM that RequirementParserTool invokes to interpret images embedded
in requirement documents.  Mirrors ``KnowledgeConfig``'s shape:

* One active row per user (system-shared rows are not supported in
  F020 — the field is reserved but unused, matching F017's design).
* ``api_key_encrypted`` stores a Fernet ciphertext; the plaintext key
  is decrypted only inside the per-user cache and never returned to
  the frontend or logged.
* ``api_key_masked`` is the only key representation that crosses the
  HTTP boundary.

``enable_in_doc_parsing`` is the user-facing switch surfaced in the
Settings page ("启用文档图片理解").  ``last_test_*`` columns mirror
the connection-test contract used by KnowledgeConfigService so the
frontend can render a status pill.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ImageUnderstandingConfig(Base):
    __tablename__ = "image_understanding_configs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    # Ownership — NULL would mean a system-shared row. F020 ships as
    # user-only; the column exists for symmetry with F017.
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))

    api_base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_masked: Mapped[str | None] = mapped_column(String(64), nullable=True)

    model_name: Mapped[str] = mapped_column(String(255), default="qwen-vl-plus", nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # F020 user-facing toggle — controls whether the vision LLM is
    # invoked during requirement parsing.  When False, OCR still runs
    # (default) and OCR text is inserted into the prompt, but the
    # vision step is skipped.
    enable_in_doc_parsing: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[str] = mapped_column(String(32), default="active")

    last_test_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    owner = relationship("User", back_populates="image_understanding_configs")

# 模块定位:ImageUnderstandingConfig 模型(F020 图片理解 per-user 配置)
#
# 字段:
#   - user_internal_id(provider / model / prompt_template / max_tokens)
#   - temperature / top_p
#   - test_endpoint(用于路由 /api/v1/image-understanding/config/test)
#
# 链路:
#   ImageUnderstandingConfigService.upsert / get_for_user
#     → VisionService.understand(...) 读 per-user 配置
#
# 关键约束:
#   - prompt_template 留空 → 走系统默认;
#   - max_tokens / temperature / top_p 三者均可在设置中调;
#   - 切 provider 时清缓存(feature flag)。
