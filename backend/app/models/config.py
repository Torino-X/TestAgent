"""Configuration models — model_configs, knowledge_base_configs, system_configs."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ModelConfig(Base):
    __tablename__ = "model_configs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    config_name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    api_base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    temperature: Mapped[float | None] = mapped_column(Float)
    max_tokens: Mapped[int | None] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    enable_thinking: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_vision: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="active")
    # CE-01: 能力配置启用开关（多能力管理，enabled/disabled）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")

    # ── CE-01: Context Engine 能力字段 ──────────────────────────────
    # 模型能力类型: chat / reasoning / embedding / reranker / compression /
    # memory_extraction。默认 'chat' 保持既有行语义。
    capability_type: Mapped[str] = mapped_column(String(32), default="chat", server_default="chat")
    # Provider / 模型能力元数据（Budget Manager 消费）
    context_window_tokens: Mapped[int | None] = mapped_column(BigInteger)
    default_max_output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    tokenizer_name: Mapped[str | None] = mapped_column(String(128))
    tokenizer_revision: Mapped[str | None] = mapped_column(String(64))
    provider_overhead_tokens: Mapped[int | None] = mapped_column(Integer)
    # Embedding 配置（维度配置化，不硬编码 1024）
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)
    normalize_embeddings: Mapped[bool | None] = mapped_column(Boolean)
    # Reranker 配置
    rerank_instruction: Mapped[str | None] = mapped_column(String(512))
    pre_rerank_limit: Mapped[int | None] = mapped_column(Integer)
    score_type: Mapped[str | None] = mapped_column(String(32))
    # 能力来源与验证时间（审计）
    capability_source: Mapped[str | None] = mapped_column(String(32))
    capability_verified_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    owner = relationship("User", back_populates="model_configs")


class KnowledgeBaseConfig(Base):
    __tablename__ = "knowledge_base_configs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    config_name: Mapped[str] = mapped_column(String(100), nullable=False)
    api_base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    knowledge_base_id: Mapped[str | None] = mapped_column(String(255))
    default_top_k: Mapped[int] = mapped_column(Integer, default=5)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default="active")

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    owner = relationship("User", back_populates="knowledge_base_configs")


class SystemConfig(Base):
    __tablename__ = "system_configs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    config_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    config_value: Mapped[str | None] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(32), default="string")
    description: Mapped[str | None] = mapped_column(String(255))
    editable: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


# 模块定位:配置模型总和(19 张基表之外的"配置侧")
#
# 包含:
#   - ModelConfig(per-user 模型配置)
#   - SystemConfig(系统级,如 rate limiting、章节默认长度)
#   - ImageUnderstandingConfig(见同名模型)
#
# 链路:
#   api/v1/settings PUT → 写对应 row
#   LLM 上游读取 → settings_service.build_llm_config_provider(user_id)
#
# 关键约束:
#   - ModelConfig 必须 per-user,DB-only(F015 后不再有 env fallback);
#   - SystemConfig 极少变动,改要 restart 服务;
#   - 加密字段存 _encrypted 后缀(密码、API key)。
