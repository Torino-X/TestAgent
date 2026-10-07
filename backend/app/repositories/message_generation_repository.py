"""Repository for :class:`AssistantMessageGeneration`.

Manages the ``assistant_message_generations`` table.  All operations are
transactional — no commits inside the repo; the caller's ``AsyncSession``
handles commit/rollback.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message_generation import AssistantMessageGeneration
from app.repositories.base import BaseRepository
from app.utils.ids import generate_public_id


class MessageGenerationRepository(BaseRepository[AssistantMessageGeneration]):
    model = AssistantMessageGeneration

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def next_generation_no(self, message_id: int) -> int:
        """Return the next ``generation_no`` for a given message.

        Uses ``SELECT MAX(generation_no)`` + 1; safe under a serializable
        isolation level or a row-level lock on the message row (the caller
        should hold ``SELECT ... FOR UPDATE`` on the ``messages`` row when
        calling this inside a regenerate transaction).
        """
        result = await self.session.execute(
            text(
                "SELECT MAX(generation_no) FROM assistant_message_generations "
                "WHERE message_id = :mid"
            ),
            {"mid": message_id},
        )
        row = result.first()
        current_max = int(row[0]) if row and row[0] is not None else 0
        return current_max + 1

    async def get_active_generation(
        self, message_id: int
    ) -> Optional[AssistantMessageGeneration]:
        result = await self.session.execute(
            text(
                "SELECT id, public_id, message_id, generation_no, status, "
                "is_active, error_code, created_at, updated_at "
                "FROM assistant_message_generations "
                "WHERE message_id = :mid AND is_active = 1 LIMIT 1"
            ),
            {"mid": message_id},
        )
        row = result.first()
        if row is None:
            return None
        gen = AssistantMessageGeneration()
        gen.id = int(row[0])
        gen.public_id = str(row[1])
        gen.message_id = int(row[2])
        gen.generation_no = int(row[3])
        gen.status = str(row[4])
        gen.is_active = bool(row[5])
        gen.error_code = str(row[6]) if row[6] else None
        gen.created_at = row[7]
        gen.updated_at = row[8]
        return gen

    async def get_by_public_id(self, public_id: str) -> Optional[AssistantMessageGeneration]:
        result = await self.session.execute(
            text(
                "SELECT id, public_id, message_id, generation_no, status, "
                "is_active, error_code, content_markdown, created_at, updated_at "
                "FROM assistant_message_generations "
                "WHERE public_id = :pid"
            ),
            {"pid": public_id},
        )
        row = result.first()
        if row is None:
            return None
        gen = AssistantMessageGeneration()
        gen.id = int(row[0])
        gen.public_id = str(row[1])
        gen.message_id = int(row[2])
        gen.generation_no = int(row[3])
        gen.status = str(row[4])
        gen.is_active = bool(row[5])
        gen.error_code = str(row[6]) if row[6] else None
        gen.content_markdown = str(row[7]) if row[7] else None
        gen.created_at = row[8]
        gen.updated_at = row[9]
        return gen

    async def create_generation(
        self,
        *,
        message_id: int,
        generation_no: int,
        status: str = "pending",
        is_active: bool = True,
    ) -> AssistantMessageGeneration:
        new_public_id = generate_public_id("gen")
        await self.session.execute(
            text(
                "INSERT INTO assistant_message_generations "
                "(public_id, message_id, generation_no, status, is_active, "
                " created_at, updated_at) "
                "VALUES (:pid, :mid, :gn, :st, :ia, NOW(), NOW())"
            ),
            {
                "pid": new_public_id,
                "mid": message_id,
                "gn": generation_no,
                "st": status,
                "ia": is_active,
            },
        )
        await self.session.flush()
        # Return the generation via public_id lookup (avoids relying on
        # LAST_INSERT_ID with raw SQL).
        return (await self.get_by_public_id(new_public_id))  # type: ignore[return-value]

    async def deactivate_all_for_message(self, message_id: int) -> None:
        """Set ``is_active = 0`` on every generation for a given message.

        Called before activating a newly-completed generation so there is
        exactly one active version per message at all times.
        """
        await self.session.execute(
            text(
                "UPDATE assistant_message_generations "
                "SET is_active = 0, updated_at = NOW() "
                "WHERE message_id = :mid AND is_active = 1"
            ),
            {"mid": message_id},
        )

    async def activate_generation(self, generation_id: int) -> None:
        await self.session.execute(
            text(
                "UPDATE assistant_message_generations "
                "SET is_active = 1, updated_at = NOW() "
                "WHERE id = :gid"
            ),
            {"gid": generation_id},
        )

    async def update_status(
        self,
        generation_id: int,
        *,
        status: str,
        content_markdown: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        updates = {"status": status, "gid": generation_id}
        if content_markdown is not None:
            await self.session.execute(
                text(
                    "UPDATE assistant_message_generations "
                    "SET status = :st, content_markdown = :cm, updated_at = NOW() "
                    "WHERE id = :gid"
                ),
                {"st": status, "cm": content_markdown, "gid": generation_id},
            )
        elif error_code is not None:
            await self.session.execute(
                text(
                    "UPDATE assistant_message_generations "
                    "SET status = :st, error_code = :ec, updated_at = NOW() "
                    "WHERE id = :gid"
                ),
                {"st": status, "ec": error_code, "gid": generation_id},
            )
        else:
            await self.session.execute(
                text(
                    "UPDATE assistant_message_generations "
                    "SET status = :st, updated_at = NOW() "
                    "WHERE id = :gid"
                ),
                {"st": status, "gid": generation_id},
            )


# 模块定位:MessageGeneration 仓储(LLM 生成审计)
#
# 链路:
#   LLM 调用后 → MessageGenerationService.record → write
#
# 关键约束:
#   - 1 message_id → 1 row(用于 audit 回查);
#   - prompt_hash 用稳定 hash 便于跨实例聚合;
#   - **不**存 raw prompt / raw response。
