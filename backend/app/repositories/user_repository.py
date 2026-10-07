"""User repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_id(self, user_id: int) -> User | None:
        result = await self.session.execute(
            select(User).where(User.id == user_id, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def get_by_public_id(self, public_id: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.public_id == public_id, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def get_by_username(self, username: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.username == username, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.email == email, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def create(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        return user


# 模块定位:User 仓储(用户表 CRUD)
#
# 链路:
#   AuthService.register/login → crypto + UserRepository.upsert
#   AuthService.get_by_internal_id / get_by_email → UserRepository
#
# 关键约束:
#   - 按 email 唯一索引查询(快);
#   - 切密码 update 必须 commit 后,token 黑名单(in-memory);
#   - 不要写 orm 复杂 join,放 service 层。
