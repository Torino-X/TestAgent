"""Conversation repository."""

from __future__ import annotations

from sqlalchemy import and_, select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.project import Project
from app.repositories.base import BaseRepository, ensure_model_id


class ConversationRepository(BaseRepository[Conversation]):
    model = Conversation

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_by_user(self, user_id: int, limit: int = 50) -> list[Conversation]:
        result = await self.session.execute(
            select(Conversation, Project.public_id, Project.name)
            .outerjoin(
                Project,
                and_(
                    Conversation.project_id == Project.id,
                    Project.user_id == user_id,
                    Project.deleted_at.is_(None),
                ),
            )
            .where(Conversation.user_id == user_id, Conversation.deleted_at.is_(None))
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        )
        conversations: list[Conversation] = []
        for conversation, project_public_id, project_name in result.all():
            # These transient values enrich the sidebar DTO without adding an
            # ORM relationship or an N+1 query.
            conversation._project_public_id = project_public_id
            conversation._project_name = project_name
            conversations.append(conversation)
        return conversations

    async def count_by_user(self, user_id: int) -> int:
        result = await self.session.execute(
            select(func.count(Conversation.id)).where(
                Conversation.user_id == user_id, Conversation.deleted_at.is_(None)
            )
        )
        return result.scalar_one()

    async def list_by_project(self, user_id: int, project_id: int, limit: int = 200) -> list[Conversation]:
        result = await self.session.execute(
            select(Conversation)
            .where(
                Conversation.user_id == user_id,
                Conversation.project_id == project_id,
                Conversation.deleted_at.is_(None),
            )
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_owned_by_public_id(self, public_id: str, user_id: int) -> Conversation | None:
        result = await self.session.execute(
            select(Conversation).where(
                Conversation.public_id == public_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def detach_project(self, project_id: int, now: object) -> None:
        await self.session.execute(
            update(Conversation)
            .where(Conversation.project_id == project_id, Conversation.deleted_at.is_(None))
            .values(project_id=None, updated_at=now)
        )

    async def get_by_public_id(self, public_id: str) -> Conversation | None:
        result = await self.session.execute(
            select(Conversation).where(
                Conversation.public_id == public_id, Conversation.deleted_at.is_(None)
            )
        )
        return result.scalar_one_or_none()

    async def get_public_id_by_internal_id(
        self, conversation_id: int
    ) -> str | None:
        """Resolve a conversation's public_id from its internal primary key.

        Used by flows that only hold an internal FK (e.g. a Message row)
        but must call APIs that key on the public id.
        """
        result = await self.session.execute(
            select(Conversation.public_id).where(
                Conversation.id == conversation_id,
                Conversation.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def create(self, conv: Conversation) -> Conversation:
        await ensure_model_id(self.session, Conversation, conv)
        self.session.add(conv)
        await self.session.flush()
        return conv

    async def update_title(self, public_id: str, title: str, updated_at: object | None = None) -> None:
        values = {"title": title}
        if updated_at is not None:
            values["updated_at"] = updated_at
        await self.session.execute(
            update(Conversation)
            .where(Conversation.public_id == public_id)
            .values(**values)
        )

    async def soft_delete(self, public_id: str, now: object) -> None:
        await self.session.execute(
            update(Conversation)
            .where(Conversation.public_id == public_id)
            .values(deleted_at=now)
        )


# 模块定位:Conversation 仓储
#
# 链路:
#   ConversationService.create / list / get / soft_delete
#     → Conversation ORM 行读写
#
# 关键约束:
#   - list 走 user_internal_id + updated_at DESC 复合索引;
#   - soft_delete 不删行,update archived_at;
#   - increment_message_count 与 ORM 行更新必须在同一事务。
