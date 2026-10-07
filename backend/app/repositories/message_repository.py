"""Message repository."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message
from app.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


# Phase 2.9A.26/2.9A.37: single stable timeline-ordering contract shared by
# every message-query path (list_messages, latest-message checks, prompt
# lookups).  ``conversation_sequence`` is the authoritative key — MySQL
# ``created_at`` is ``DATETIME`` (second resolution), so a user/agent pair
# written in the same tick collides and ``ORDER BY created_at`` alone is an
# undefined filesort tie.  ``created_at`` stays as a legacy fallback for
# pre-sequence rows, and ``id`` is the deterministic final tiebreaker.
#
# ASC  → the list_messages order (oldest → newest).
# DESC → newest-first; used by the regeneration latest-message check so it
#        picks the exact row the front-end renders as the last message.


class MessageRepository(BaseRepository[Message]):
    model = Message
    _SEQUENCE_INSERT_ATTEMPTS = 3

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    @staticmethod
    def _timeline_order(direction: str = "ASC") -> str:
        """Stable timeline ORDER BY clause.

        ``conversation_sequence IS NULL`` stays ascending in both
        directions: legacy rows without a sequence always sort last,
        regardless of ASC/DESC.  The remaining keys take ``direction``.
        """
        if direction.upper() == "DESC":
            return (
                "conversation_sequence IS NULL ASC, "
                "conversation_sequence DESC, created_at DESC, id DESC"
            )
        return (
            "conversation_sequence IS NULL, "
            "conversation_sequence ASC, created_at ASC, id ASC"
        )

    def _dialect_name(self) -> str:
        bind = self.session.get_bind()
        return getattr(getattr(bind, "dialect", None), "name", "")

    @staticmethod
    def _is_sequence_integrity_error(exc: IntegrityError) -> bool:
        message = str(exc.orig or exc)
        return (
            "uq_messages_conversation_sequence" in message
            or "conversation_sequence" in message
            or "Duplicate entry" in message
        )

    async def _lock_conversation_for_sequence(self, conversation_id: int) -> None:
        """Serialize per-conversation sequence allocation where row locks exist."""
        from sqlalchemy import text as _t

        if self._dialect_name() not in {"mysql", "postgresql"}:
            return

        await self.session.execute(
            _t("SELECT id FROM conversations WHERE id = :cid FOR UPDATE"),
            {"cid": conversation_id},
        )

    async def _next_conversation_sequence(self, conversation_id: int) -> int:
        from sqlalchemy import text as _t

        if self._dialect_name() in {"mysql", "postgresql"}:
            seq_result = await self.session.execute(
                _t(
                    "SELECT conversation_sequence "
                    "FROM messages WHERE conversation_id = :cid "
                    "AND deleted_at IS NULL "
                    "ORDER BY conversation_sequence DESC "
                    "LIMIT 1 FOR UPDATE"
                ),
                {"cid": conversation_id},
            )
            return int(seq_result.scalar() or 0) + 1

        seq_result = await self.session.execute(
            _t(
                "SELECT COALESCE(MAX(conversation_sequence), 0) + 1 "
                "FROM messages WHERE conversation_id = :cid "
                "AND deleted_at IS NULL"
            ),
            {"cid": conversation_id},
        )
        return int(seq_result.scalar() or 1)

    async def _execute_insert(self, msg: Message, payload_str: str | None):
        from sqlalchemy import text

        return await self.session.execute(
            text(
                "INSERT INTO messages "
                "(public_id, user_id, conversation_id, task_id, role, "
                " message_type, content, payload_json, status, "
                " conversation_sequence, reply_to_message_id, "
                " created_at, updated_at) "
                "VALUES "
                "(:pid, :uid, :cid, :tid, :role, :mt, :content, :pj, :st, "
                " :seq, :rtmid, :ca, :ua)"
            ),
            {
                "pid": msg.public_id,
                "uid": msg.user_id,
                "cid": msg.conversation_id,
                "tid": msg.task_id,
                "role": msg.role,
                "mt": msg.message_type,
                "content": msg.content,
                "pj": payload_str,
                "st": msg.status,
                "seq": msg.conversation_sequence,
                "rtmid": msg.reply_to_message_id,
                "ca": msg.created_at,
                "ua": msg.updated_at,
            },
        )

    async def get_by_public_id(self, public_id: str, user_id: int) -> Message | None:
        """Look up a single non-deleted message by its public id.

        Scoped to ``user_id`` so a caller can never resolve a message that
        belongs to another user.
        """
        from sqlalchemy import text as _t
        result = await self.session.execute(
            _t(
                "SELECT * FROM messages "
                "WHERE public_id = :pid AND user_id = :uid "
                "AND deleted_at IS NULL "
                "LIMIT 1"
            ),
            {"pid": public_id, "uid": user_id},
        )
        row = result.first()
        if row is None:
            return None
        message = Message()
        for col in row._mapping.keys():
            setattr(message, col, row._mapping[col])
        return message

    async def list_by_conversation(self, user_id: int, conversation_id: int, limit: int = 200) -> list[Message]:
        # Phase 2.9A.26: stable ordering by conversation_sequence ASC,
        # with created_at + id as deterministic tiebreakers.  The new
        # index ix_messages_conversation_sequence keeps this query
        # off the filesort path.
        from sqlalchemy import text as _t
        result = await self.session.execute(
            _t(
                "SELECT * FROM messages "
                "WHERE user_id = :uid AND conversation_id = :cid "
                "AND deleted_at IS NULL "
                f"ORDER BY {self._timeline_order('ASC')} "
                "LIMIT :lim"
            ),
            {"uid": user_id, "cid": conversation_id, "lim": limit},
        )
        rows = result.fetchall()
        # Build ORM Message objects from raw rows
        messages: list[Message] = []
        for row in rows:
            m = Message()
            for col in row._mapping.keys():
                setattr(m, col, row._mapping[col])
            messages.append(m)
        return messages

    async def list_after_id(
        self,
        user_id: int,
        conversation_id: int,
        after_message_id: int,
        *,
        limit: int = 200,
    ) -> list[Message]:
        """Return the next owner-scoped conversation messages after a summary cursor.

        Conversation summaries are maintained incrementally.  A cursor is
        required so a refresh processes only turns not already represented by
        the active summary instead of repeatedly selecting the oldest page.
        """
        from sqlalchemy import text as _t

        result = await self.session.execute(
            _t(
                "SELECT * FROM messages "
                "WHERE user_id = :uid AND conversation_id = :cid "
                "AND deleted_at IS NULL AND id > :after_id "
                f"ORDER BY {self._timeline_order('ASC')} "
                "LIMIT :lim"
            ),
            {
                "uid": user_id,
                "cid": conversation_id,
                "after_id": after_message_id,
                "lim": limit,
            },
        )
        messages: list[Message] = []
        for row in result.fetchall():
            message = Message()
            for col in row._mapping.keys():
                setattr(message, col, row._mapping[col])
            messages.append(message)
        return messages

    async def get_latest_by_conversation(
        self, user_id: int, conversation_id: int
    ) -> Message | None:
        """Return the last user-visible message in a conversation.

        Mirrors ``list_by_conversation`` (the timeline contract) reversed:
        the single newest non-deleted row by
        ``conversation_sequence DESC, created_at DESC, id DESC``.  This is
        the message the front-end renders at the bottom of the timeline.
        """
        from sqlalchemy import text as _t
        result = await self.session.execute(
            _t(
                "SELECT * FROM messages "
                "WHERE user_id = :uid AND conversation_id = :cid "
                "AND deleted_at IS NULL "
                f"ORDER BY {self._timeline_order('DESC')} "
                "LIMIT 1"
            ),
            {"uid": user_id, "cid": conversation_id},
        )
        row = result.first()
        if row is None:
            return None
        message = Message()
        for col in row._mapping.keys():
            setattr(message, col, row._mapping[col])
        return message

    async def get_latest_user_text_by_conversation(
        self, user_id: int, conversation_id: int
    ) -> Message | None:
        """Return the newest user_text message in a conversation.

        Used as the legacy fallback for resolving the original user prompt
        of an agent reply whose ``reply_to_message_id`` anchor is missing.
        Ordering is the same stable timeline contract.
        """
        from sqlalchemy import text as _t
        result = await self.session.execute(
            _t(
                "SELECT * FROM messages "
                "WHERE user_id = :uid AND conversation_id = :cid "
                "AND role = 'user' AND message_type = 'user_text' "
                "AND deleted_at IS NULL "
                f"ORDER BY {self._timeline_order('DESC')} "
                "LIMIT 1"
            ),
            {"uid": user_id, "cid": conversation_id},
        )
        row = result.first()
        if row is None:
            return None
        message = Message()
        for col in row._mapping.keys():
            setattr(message, col, row._mapping[col])
        return message

    async def list_recent_by_conversation(
        self,
        user_id: int,
        conversation_id: int,
        limit: int = 12,
        exclude_id: int | None = None,
    ) -> list[Message]:
        """Fetch the most recent *limit* user/agent text messages, newest first.

        Returns them in chronological (oldest-first) order so that the
        caller can feed them directly into context construction.
        Excludes ``exclude_id`` (typically the message the user just sent)
        to avoid duplication in the context window.
        """
        stmt = (
            select(Message)
            .where(
                Message.user_id == user_id,
                Message.conversation_id == conversation_id,
                Message.role.in_(("user", "agent")),
                Message.message_type.in_(("user_text", "agent_text")),
                Message.deleted_at.is_(None),
            )
            .order_by(
                Message.conversation_sequence.desc(),
                Message.created_at.desc(),
                Message.id.desc(),
            )
            .limit(limit)
        )
        if exclude_id is not None:
            stmt = stmt.where(Message.id != exclude_id)

        result = await self.session.execute(stmt)
        rows = list(result.scalars().all())
        rows.reverse()  # chronological order (oldest first)
        return rows

    async def list_context_messages_by_conversation(
        self,
        user_id: int,
        conversation_id: int,
        *,
        exclude_id: int | None = None,
    ) -> list[Message]:
        """Return the complete user/assistant text timeline in stable order.

        This is intentionally distinct from ``list_by_conversation``: context
        construction must not pull task events or hidden message types, and it
        needs to exclude the just-persisted current user message when present.
        The caller uses this only before a durable conversation summary exists;
        after compaction it returns to the bounded recent-window query.
        """
        stmt = (
            select(Message)
            .where(
                Message.user_id == user_id,
                Message.conversation_id == conversation_id,
                Message.role.in_(("user", "agent")),
                Message.message_type.in_(("user_text", "agent_text")),
                Message.deleted_at.is_(None),
            )
            .order_by(
                Message.conversation_sequence.asc(),
                Message.created_at.asc(),
                Message.id.asc(),
            )
        )
        if exclude_id is not None:
            stmt = stmt.where(Message.id != exclude_id)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_conversation(self, conversation_id: int) -> int:
        """Count all non-deleted messages in a conversation."""
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count())
            .select_from(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.deleted_at.is_(None),
            )
        )
        return result.scalar() or 0

    async def count_group_by_conversation(
        self, user_id: int, conversation_ids: list[int]
    ) -> dict[int, int]:
        """Batch count messages grouped by conversation_id (Phase 0 — list view).

        Replaces the per-conversation COUNT(*) loop that produced
        ``2 + 2N`` SQL for ``ConversationService.list_conversations``.
        Single SELECT with GROUP BY; only non-deleted messages are counted.
        Returns a dict keyed by conversation_id; missing conversations
        simply absent from the dict (caller should treat as 0).
        """
        from sqlalchemy import func

        if not conversation_ids:
            return {}
        result = await self.session.execute(
            select(Message.conversation_id, func.count())
            .where(
                Message.user_id == user_id,
                Message.conversation_id.in_(conversation_ids),
                Message.deleted_at.is_(None),
            )
            .group_by(Message.conversation_id)
        )
        return {row[0]: int(row[1]) for row in result.all()}

    async def create(self, msg: Message) -> Message:
        # aiomysql driver chokes on dict params for JSON columns —
        # use raw INSERT with explicit json.dumps.
        import json as _json
        payload_str = None
        if msg.payload_json is not None:
            if isinstance(msg.payload_json, str):
                payload_str = msg.payload_json
            else:
                payload_str = _json.dumps(msg.payload_json, ensure_ascii=False, default=str)

        # Phase 2.9A.26+: assign conversation_sequence when not
        # explicitly provided.  Production databases lock the conversation
        # and current tail message before allocation; the retry loop below
        # is the final guard for rare conflicts.
        # MAX query — the second writer's MAX will already include
        should_allocate_sequence = (
            msg.conversation_sequence is None and msg.conversation_id is not None
        )

        result = None
        last_sequence_error: IntegrityError | None = None

        for attempt in range(1, self._SEQUENCE_INSERT_ATTEMPTS + 1):
            if should_allocate_sequence:
                await self._lock_conversation_for_sequence(msg.conversation_id)
                msg.conversation_sequence = await self._next_conversation_sequence(
                    msg.conversation_id
                )

            try:
                async with self.session.begin_nested():
                    result = await self._execute_insert(msg, payload_str)
                    await self.session.flush()
                break
            except IntegrityError as exc:
                if not should_allocate_sequence or not self._is_sequence_integrity_error(exc):
                    raise
                last_sequence_error = exc
                logger.warning(
                    "FALLBACK_USED | component=message_repository "
                    "operation=create reason=conversation_sequence_conflict "
                    "conversation_id=%s attempted_sequence=%s attempt=%s/%s",
                    msg.conversation_id,
                    msg.conversation_sequence,
                    attempt,
                    self._SEQUENCE_INSERT_ATTEMPTS,
                )
                msg.conversation_sequence = None

        if result is None:
            assert last_sequence_error is not None
            raise last_sequence_error

        # raw INSERT bypasses ORM autoincrement backfill, so msg.id would
        # stay None unless read back explicitly (same pattern as
        # AgentTaskRepository.create). lastrowid is dialect-agnostic
        # (MySQL + SQLite) and lets callers anchor reply_to_message_id /
        # current_message_id on a real primary key.
        lastrowid = result.lastrowid
        if lastrowid is not None:
            msg.id = int(lastrowid)
        return msg


# 模块定位:Message 仓储(消息 CRUD + 时间线查询)
#
# 链路:
#   MessageService.send_message → 写 user + assistant pair
#   api/v1/conversations/{id}/messages → list_by_conversation
#   useTaskEventRestore(rebuild) → list_by_conversation(全量)
#
# 关键约束:
#   - 必须按 (conversation_id, conversation_sequence ASC) 查询;
#   - increment_message_count → 写消息 + 计数触发器同步;
#   - feedback_count 类似;
#   - regen 不直接改 row,新写一条 + 软隐藏旧。
