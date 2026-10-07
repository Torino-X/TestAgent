"""F017 — Company knowledge-base configuration model.

Stores per-user (or system-shared) connection settings for the
internal MaaS knowledge base.  ``api_key_encrypted`` holds a Fernet
ciphertext; the masked form is the only representation that ever
leaves the API layer.

A system-shared row (user_id IS NULL) may exist for environment-level
provisioning; user-level rows take precedence when present.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class KnowledgeConfig(Base):
    __tablename__ = "knowledge_configs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    # Ownership — NULL means a system-shared row used as fallback when
    # the user has not configured their own.
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))

    scope: Mapped[str] = mapped_column(String(32), default="system", nullable=False)

    api_base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_masked: Mapped[str | None] = mapped_column(String(64), nullable=True)

    default_knowledge_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    top_k: Mapped[int] = mapped_column(Integer, default=5)
    similarity_threshold: Mapped[float] = mapped_column(Float, default=0.35)
    retrieve_strategy: Mapped[int] = mapped_column(Integer, default=3)

    enable_rerank_model: Mapped[bool] = mapped_column(Boolean, default=True)
    rerank_model: Mapped[str | None] = mapped_column(String(128), default="bge-reranker-v2-m3")
    knowledge_graph: Mapped[bool] = mapped_column(Boolean, default=False)

    direct_answer_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    test_plan_generation_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    test_case_generation_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)

    status: Mapped[str] = mapped_column(String(32), default="active")
    last_test_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    owner = relationship("User", back_populates="knowledge_configs")

# 模块定位:KnowledgeConfig 模型(F017 KB 配置,per-user)
#
# 字段:
#   - id / user_internal_id(可空 — system_shared 行)
#   - api_key_encrypted(加密后存的,**绝不存明文**)
#   - default_knowledge_ids / top_k / similarity_threshold
#   - retrieve_strategy(1=向量 / 2=关键词 / 3=混合)
#   - rerank_model / enable_rerank_model
#   - default_test_plan_generation_enabled(向后兼容)
#
# 链路:
#   KnowledgeConfigService.get_for_user(user_id) → 按 user 优先 / shared 兜底
#   api/v1/settings PUT → encrypt_api_key + write
#
# 关键约束:
#   - api_key 必须走 crypto.encrypt_api_key(...);
#   - user_internal_id 可空,表示 system-shared 配置;
#   - 修改时若 active KB 上有 in-flight retrieval,等其完成再切换。
