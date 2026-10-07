"""Phase 0 — ConversationService.list_conversations SQL count guard.

Regression test for the ``2 + 2N`` SQL collapse required by the
Redis cache audit (cold path must be ``<= 4`` SQL regardless of the
number of conversations).

Asserts on a per-test SQLAlchemy event counter so the assertions
stay independent of dialect quirks and don't require a real MySQL
binary.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.uploaded_file import UploadedFile
from app.models.user import User
from app.services.conversation_service import ConversationService


def _now() -> datetime:
    return datetime(2026, 9, 6, 12, 0, 0)


# ── SQL counter ───────────────────────────────────────────────────


class _SqlCounter:
    """Counts SELECT / INSERT / UPDATE / DELETE statements on a session.

    We attach an event listener to the sync engine underlying the
    async session.  ``before_cursor_execute`` fires for *every*
    statement (including SELECTs) so a simple counter is enough to
    assert the Phase 0 SQL budget.
    """

    def __init__(self, sync_engine) -> None:
        self._sync_engine = sync_engine
        self._counts: dict[str, int] = {
            "SELECT": 0,
            "INSERT": 0,
            "UPDATE": 0,
            "DELETE": 0,
            "OTHER": 0,
        }

    def __enter__(self):
        self._listener = event.listen(
            self._sync_engine,
            "before_cursor_execute",
            self._on_execute,
        )
        return self

    def __exit__(self, exc_type, exc, tb):
        event.remove(self._sync_engine, "before_cursor_execute", self._on_execute)

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        head = statement.lstrip().split(None, 1)[0].upper()
        if head in self._counts:
            self._counts[head] += 1
        else:
            self._counts["OTHER"] += 1

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    def get(self, op: str) -> int:
        return self._counts[op]


# ── helpers ───────────────────────────────────────────────────────


async def _seed_user(session: AsyncSession, *, user_id: int = 1) -> None:
    session.add(
        User(
            id=user_id,
            public_id=f"user_{user_id}",
            username=f"user{user_id}",
            password_hash="hash",
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await session.flush()


async def _seed_conversation(
    session: AsyncSession,
    *,
    conv_id: int,
    user_id: int,
    title: str = "C",
    deleted_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> None:
    session.add(
        Conversation(
            id=conv_id,
            public_id=f"conv_{conv_id}",
            user_id=user_id,
            title=title,
            created_at=_now(),
            updated_at=updated_at or _now(),
            deleted_at=deleted_at,
        )
    )
    await session.flush()


_FILE_COUNTER = {"next_id": 1000}


def _next_file_id() -> int:
    _FILE_COUNTER["next_id"] += 1
    return _FILE_COUNTER["next_id"]


async def _seed_message(
    session: AsyncSession,
    *,
    conv_id: int,
    user_id: int,
    deleted_at: datetime | None = None,
    msg_idx: int = 0,
) -> None:
    session.add(
        Message(
            public_id=f"msg_c{conv_id}_u{user_id}_i{msg_idx}",
            user_id=user_id,
            conversation_id=conv_id,
            role="user",
            message_type="user_text",
            content="x",
            created_at=_now(),
            updated_at=_now(),
            deleted_at=deleted_at,
        )
    )
    await session.flush()


async def _seed_file(
    session: AsyncSession,
    *,
    conv_id: int,
    user_id: int,
    deleted_at: datetime | None = None,
    file_idx: int = 0,
) -> None:
    # UploadedFile uses BigInteger autoincrement which SQLite does not
    # auto-fill — we must seed an explicit id.  Repository.create()
    # calls ``ensure_model_id`` which assigns one when running on SQLite
    # tests.  Calling through the repo keeps the test path identical
    # to production code paths.
    from app.repositories.file_repository import FileRepository

    f = UploadedFile(
        public_id=f"file_c{conv_id}_u{user_id}_i{file_idx}",
        user_id=user_id,
        conversation_id=conv_id,
        original_name="x.bin",
        stored_name="x",
        file_ext=".bin",
        file_size=1,
        file_type="unknown",
        storage_type="local",
        storage_path=f"/tmp/x_c{conv_id}_i{file_idx}",
        created_at=_now(),
        updated_at=_now(),
        deleted_at=deleted_at,
    )
    await FileRepository(session).create(f)
    await session.flush()


# ── tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_conversations_empty_user_uses_2_sql(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as session:
        await _seed_user(session)
        await session.commit()

    async with sqlite_session_factory() as session:
        service = ConversationService(session)
        with _SqlCounter(session.get_bind()) as c:
            summaries, total = await service.list_conversations(user_internal_id=1)

    assert summaries == []
    assert total == 0
    # Empty path: list_by_user (1) + count_by_user (1) = 2 SQL
    # (the two GROUP BY batch counts are correctly skipped).
    assert c.get("SELECT") == 2, f"empty path should be 2 SELECTs, got {c.get('SELECT')}"


@pytest.mark.asyncio
async def test_list_conversations_one_user_uses_4_sql(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as session:
        await _seed_user(session)
        await _seed_conversation(session, conv_id=1, user_id=1)
        await _seed_message(session, conv_id=1, user_id=1, msg_idx=0)
        await _seed_file(session, conv_id=1, user_id=1, file_idx=0)
        await session.commit()

    async with sqlite_session_factory() as session:
        service = ConversationService(session)
        with _SqlCounter(session.get_bind()) as c:
            summaries, total = await service.list_conversations(user_internal_id=1)

    assert total == 1
    assert len(summaries) == 1
    assert summaries[0]["message_count"] == 1
    assert summaries[0]["file_count"] == 1
    # Populated path: list_by_user + count_by_user + GROUP BY msg +
    # GROUP BY file = 4 SELECTs.
    assert c.get("SELECT") == 4, (
        f"single-conversation path should be 4 SELECTs, got {c.get('SELECT')}; "
        f"counts={c._counts}"
    )


@pytest.mark.asyncio
async def test_list_conversations_50_users_still_4_sql(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as session:
        await _seed_user(session)
        for i in range(1, 51):
            await _seed_conversation(session, conv_id=i, user_id=1)
            await _seed_message(session, conv_id=i, user_id=1, msg_idx=0)
            await _seed_message(session, conv_id=i, user_id=1, msg_idx=1)
            await _seed_file(session, conv_id=i, user_id=1, file_idx=0)
        await session.commit()

    async with sqlite_session_factory() as session:
        service = ConversationService(session)
        with _SqlCounter(session.get_bind()) as c:
            summaries, total = await service.list_conversations(user_internal_id=1)

    assert total == 50
    assert len(summaries) == 50
    # Every summary should report 2 messages / 1 file.
    for s in summaries:
        assert s["message_count"] == 2
        assert s["file_count"] == 1
    # CRITICAL: even with 50 conversations, SQL count stays flat at 4.
    # The pre-Phase-0 code would have produced 2 + 2*50 = 102 SELECTs.
    assert c.get("SELECT") == 4, (
        f"50-conversation path must stay at 4 SELECTs (Phase 0 invariant); "
        f"got {c.get('SELECT')} — pre-Phase-0 would be 102"
    )


@pytest.mark.asyncio
async def test_list_conversations_soft_deleted_excluded(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as session:
        await _seed_user(session)
        # 3 active + 2 soft-deleted
        for i in range(1, 4):
            await _seed_conversation(session, conv_id=i, user_id=1)
        for i in range(4, 6):
            await _seed_conversation(
                session,
                conv_id=i,
                user_id=1,
                deleted_at=_now(),
            )
        await session.commit()

    async with sqlite_session_factory() as session:
        service = ConversationService(session)
        summaries, total = await service.list_conversations(user_internal_id=1)

    assert total == 3
    assert len(summaries) == 3
    assert {s["id"] for s in summaries} == {"conv_1", "conv_2", "conv_3"}


@pytest.mark.asyncio
async def test_list_conversations_soft_deleted_messages_files_excluded(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as session:
        await _seed_user(session)
        await _seed_conversation(session, conv_id=1, user_id=1)
        # 2 active messages + 1 deleted message → group-by must skip
        # the deleted row.
        await _seed_message(session, conv_id=1, user_id=1, msg_idx=0)
        await _seed_message(session, conv_id=1, user_id=1, msg_idx=1)
        await _seed_message(session, conv_id=1, user_id=1, msg_idx=2, deleted_at=_now())
        # 1 active file + 1 deleted file
        await _seed_file(session, conv_id=1, user_id=1, file_idx=0)
        await _seed_file(session, conv_id=1, user_id=1, file_idx=1, deleted_at=_now())
        await session.commit()

    async with sqlite_session_factory() as session:
        service = ConversationService(session)
        summaries, _ = await service.list_conversations(user_internal_id=1)

    assert summaries[0]["message_count"] == 2
    assert summaries[0]["file_count"] == 1
