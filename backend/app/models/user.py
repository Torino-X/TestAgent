"""User model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    # relationships
    conversations = relationship("Conversation", back_populates="owner")
    model_configs = relationship("ModelConfig", back_populates="owner")
    knowledge_base_configs = relationship("KnowledgeBaseConfig", back_populates="owner")
    knowledge_configs = relationship("KnowledgeConfig", back_populates="owner")
    image_understanding_configs = relationship("ImageUnderstandingConfig", back_populates="owner")


# 模块定位:User 模型(用户账号基础信息)
#
# 字段:
#   - 公开 id(public_id 字符串)
#   - 内部 id(internal_id int, 数据库主键)
#   - email(唯一)
#   - password_hash(crypto 模块生成,**不存明文**)
#   - created_at / updated_at / is_admin (per-user 管理员)
#
# 链路:
#   AuthService.register/login → crypto.hash_password → 写 User 行
#   deps.get_current_user → 取 internal_id → 注入 services
#
# 关键约束:
#   - password_hash 必须用 app.core.crypto 模块;
#   - email 唯一索引(高频读);
#   - 公开 API 永远不返回 password_hash 字段;
#   - 不要放 auth_token / refresh_token 进 User 行(单独表)。
