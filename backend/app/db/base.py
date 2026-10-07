"""SQLAlchemy declarative base."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    __abstract__ = True
# db.base:DeclarativeBase 子类 + 通用 mixin(created_at / updated_at / soft delete);所有 models 继承自这里。
