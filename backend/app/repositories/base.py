"""Base repository with common CRUD patterns."""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


async def ensure_model_id(
    session: AsyncSession,
    model: type[ModelT],
    instance: Any,
) -> Any:
    """SQLite 下 BIGINT 主键不自增 → 显式分配 id。

    MySQL 的 BIGINT AUTO_INCREMENT 正常工作；此处仅在 instance.id 为空时
    用 ``MAX(id)+1`` 分配，保证 SQLite 单元测试可写行。
    """
    if getattr(instance, "id", None) is not None:
        return instance
    # MySQL 下 AUTO_INCREMENT 已经处理好,跳过手动分配避免并发主键冲突。
    # 仅在 SQLite(测试环境)下才用 MAX(id)+1。
    bind = getattr(session.bind, "dialect", None) if session.bind else None
    dialect_name = getattr(bind, "name", "") if bind else ""
    if dialect_name and "mysql" in dialect_name:
        return instance
    stmt = select(func.max(model.id))
    current_max = (await session.execute(stmt)).scalar()
    instance.id = int(current_max or 0) + 1
    return instance


class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: AsyncSession):
        self.session = session


# 模块定位:BaseRepository — 通用 CRUD 模板基类(Generic[T])
#
# 提供:
#   - get_by_id(session, id) → Optional[T]
#   - get_by_public_id(session, public_id) → Optional[T]
#   - create / update / delete 的默认实现
#   - 用 TypeVar[T] 让子类继承时拿到具体 model 类型
#
# 子类继承:
#   class UserRepository(BaseRepository[User]):
#       def __init__(self, session): super().__init__(session, User)
#
# 关键约束:
#   - 数据库 session 由调用方传入(repository 是 stateless);
#   - 不在 base 写具体业务逻辑 ——
#     跨表的复合查询放各子类;
#   - 统一软删除(若 model 有 deleted_at);
#   - 不要在 base 引 model class(避免循环依赖)。
