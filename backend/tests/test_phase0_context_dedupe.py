"""Phase 0 — ConversationContextService request-scoped dedupe.

Regression test for the
``build_intent_context + build_chat_context`` collapse.  Both
builders historically issued their own ``list_recent_by_conversation``
/ ``_get_latest_summary`` / ``build_file_summaries`` /
``build_latest_task_summary`` calls, producing 4 duplicate SELECTs
on every ``send_message`` invocation.  Phase 0 introduces
``_load_context`` which memoises the result for the lifetime of the
service instance.

The test wires a real ConversationContextService against an in-memory
SQLite fixture and asserts on a per-table SELECT counter (table name
parsed from the SQL statement head) that:

  * ``list_recent_by_conversation`` (messages table) is called only
    **once** even though both builders consume messages.
  * ``conversation_summaries`` is queried only **once** (both
    builders pull the same summary).
  * ``uploaded_files`` is queried only **once**.
  * ``agent_tasks`` is queried only **once**.

Each builder still issues the additional ``agent_events``,
``artifacts``, ``tool_calls`` and ``human_confirmations`` selects
needed to build the task summary (those are inside
``build_latest_task_summary`` and stay in scope).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_task import AgentTask
from app.models.conversation import Conversation
from app.models.conversation_summary import ConversationSummary
from app.models.message import Message
from app.models.uploaded_file import UploadedFile
from app.models.user import User
from app.services.conversation_context_service import (
    ConversationContextService,
)


def _now() -> datetime:
    return datetime(2026, 9, 6, 12, 0, 0)


# ── SQL counter ───────────────────────────────────────────────────


class _TableSelectCounter:
    """Per-table SELECT counter that parses ``FROM <table>`` / ``UPDATE <table>``.

    Only counts SELECT statements (case-insensitive head).  Tracks
    ``SELECT`` / ``INSERT`` / ``UPDATE`` / ``DELETE`` totals too for
    the assert summary.
    """

    _FROM_RE = re.compile(r"\bFROM\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", re.IGNORECASE)
    _UPDATE_RE = re.compile(r"\bUPDATE\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", re.IGNORECASE)
    _INSERT_RE = re.compile(r"\bINSERT\s+INTO\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", re.IGNORECASE)

    def __init__(self, sync_engine) -> None:
        self._sync_engine = sync_engine
        self.selects_by_table: dict[str, int] = {}
        self.totals: dict[str, int] = {"SELECT": 0, "INSERT": 0, "UPDATE": 0, "DELETE": 0}

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
        if head == "SELECT":
            self.totals["SELECT"] += 1
            m = self._FROM_RE.search(statement)
            if m:
                tbl = m.group(1)
                self.selects_by_table[tbl] = self.selects_by_table.get(tbl, 0) + 1
        elif head in ("INSERT", "UPDATE", "DELETE"):
            self.totals[head] += 1

    def selects(self, table: str) -> int:
        return self.selects_by_table.get(table, 0)


# ── seed helpers ──────────────────────────────────────────────────


async def _seed_user(session: AsyncSession, user_id: int = 1) -> None:
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


async def _seed_conversation(session: AsyncSession, *, conv_id: int, user_id: int) -> None:
    session.add(
        Conversation(
            id=conv_id,
            public_id=f"conv_{conv_id}",
            user_id=user_id,
            title="C",
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await session.flush()


async def _seed_messages(session: AsyncSession, *, conv_id: int, user_id: int, count: int = 4) -> None:
    """Seed alternating user/agent messages so both context builders
    find data.  Uses unique public_ids so the test runs reproducibly.
    """
    for i in range(count):
        session.add(
            Message(
                public_id=f"msg_c{conv_id}_i{i}",
                user_id=user_id,
                conversation_id=conv_id,
                role="user" if i % 2 == 0 else "agent",
                message_type="user_text" if i % 2 == 0 else "agent_text",
                content=f"m{i}",
                created_at=_now(),
                updated_at=_now(),
            )
        )
    await session.flush()


async def _seed_file(session: AsyncSession, *, conv_id: int, user_id: int) -> None:
    from app.repositories.file_repository import FileRepository

    f = UploadedFile(
        public_id=f"file_c{conv_id}_u{user_id}",
        user_id=user_id,
        conversation_id=conv_id,
        original_name="x.bin",
        stored_name="x",
        file_ext=".bin",
        file_size=1,
        file_type="unknown",
        storage_type="local",
        storage_path="/tmp/x",
        created_at=_now(),
        updated_at=_now(),
    )
    await FileRepository(session).create(f)
    await session.flush()


_TASK_COUNTER = {"next_id": 5000}
_SUMMARY_COUNTER = {"next_id": 7000}


async def _seed_agent_task(session: AsyncSession, *, conv_id: int, user_id: int) -> None:
    """Seed an AgentTask with an explicit id so SQLite's lack of
    BigInteger autoincrement doesn't matter for the dedupe test.

    ``AgentTaskRepository.create`` issues a raw INSERT and reads back
    ``LAST_INSERT_ID`` — that works for MySQL but SQLite + the
    ``session.refresh`` after raw insert leaves ``task.id`` unset in
    unit-test SQLite paths.  The dedupe test only needs an existing
    row, not a production-style insert.
    """
    _TASK_COUNTER["next_id"] += 1
    task = AgentTask(
        id=_TASK_COUNTER["next_id"],
        public_id=f"task_c{conv_id}",
        user_id=user_id,
        conversation_id=conv_id,
        task_type="test_plan_generation",
        status="running",
        context_version=0,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(task)
    await session.flush()


async def _seed_summary(session: AsyncSession, *, conv_id: int, user_id: int) -> None:
    _SUMMARY_COUNTER["next_id"] += 1
    session.add(
        ConversationSummary(
            id=_SUMMARY_COUNTER["next_id"],
            public_id=f"sum_c{conv_id}",
            user_id=user_id,
            conversation_id=conv_id,
            summary_text="hello",
            summary_version=1,
            status="active",
            schema_version="v1",
            recovery_mode="summary_only",
            summary_type="conversation",
            message_count=2,
            estimated_tokens=10,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await session.flush()


# ── tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_intent_then_chat_share_messages_select(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as session:
        await _seed_user(session)
        await _seed_conversation(session, conv_id=1, user_id=1)
        await _seed_messages(session, conv_id=1, user_id=1, count=4)
        await _seed_file(session, conv_id=1, user_id=1)
        await _seed_agent_task(session, conv_id=1, user_id=1)
        await _seed_summary(session, conv_id=1, user_id=1)
        await session.commit()

    async with sqlite_session_factory() as session:
        svc = ConversationContextService(session)
        with _TableSelectCounter(session.get_bind()) as c:
            intent = await svc.build_intent_context(
                user_id=1, conversation_id=1, current_message_id=None, max_turns=3
            )
            chat = await svc.build_chat_context(
                user_id=1, conversation_id=1, current_message_id=None, max_turns=10
            )

    # The two builders must succeed and share data.
    assert intent is not None
    assert chat is not None
    # CRITICAL — Phase 0 invariant: messages / uploaded_files /
    # conversation_summaries / agent_tasks are each queried **once**.
    assert c.selects("messages") == 1, (
        f"messages should be queried exactly once across both builders; "
        f"got {c.selects('messages')} (pre-Phase-0: 2).  "
        f"all selects_by_table={c.selects_by_table}"
    )
    assert c.selects("conversation_summaries") == 1, (
        f"conversation_summaries should be queried exactly once; "
        f"got {c.selects('conversation_summaries')} (pre-Phase-0: 2)"
    )
    assert c.selects("uploaded_files") == 1, (
        f"uploaded_files should be queried exactly once; "
        f"got {c.selects('uploaded_files')} (pre-Phase-0: 2)"
    )
    assert c.selects("agent_tasks") == 1, (
        f"agent_tasks should be queried exactly once; "
        f"got {c.selects('agent_tasks')} (pre-Phase-0: 2)"
    )


@pytest.mark.asyncio
async def test_intent_only_runs_once_when_builder_reinvoked(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as session:
        await _seed_user(session)
        await _seed_conversation(session, conv_id=1, user_id=1)
        await _seed_messages(session, conv_id=1, user_id=1, count=2)
        await session.commit()

    async with sqlite_session_factory() as session:
        svc = ConversationContextService(session)
        with _TableSelectCounter(session.get_bind()) as c:
            # Two invocations of the same builder should still be a
            # no-op for messages because ``_load_context`` dedupes on
            # the (user, conv, current_message_id) tuple.
            await svc.build_intent_context(
                user_id=1, conversation_id=1, current_message_id=None, max_turns=3
            )
            await svc.build_intent_context(
                user_id=1, conversation_id=1, current_message_id=None, max_turns=3
            )
        # messages queried exactly once across two builder invocations.
        assert c.selects("messages") == 1, (
            f"messages should be queried once across two builder calls; "
            f"got {c.selects('messages')}"
        )


@pytest.mark.asyncio
async def test_chat_after_intent_no_extra_messages_select(
    sqlite_session_factory,
):
    """Pre-Phase-0 ``send_message`` path: build_intent_context then
    build_chat_context.  Each used to issue its own
    ``list_recent_by_conversation`` — Phase 0 dedupes to one.  This
    test mirrors the audit's exact pattern.
    """
    async with sqlite_session_factory() as session:
        await _seed_user(session)
        await _seed_conversation(session, conv_id=1, user_id=1)
        await _seed_messages(session, conv_id=1, user_id=1, count=6)
        await _seed_file(session, conv_id=1, user_id=1)
        await _seed_agent_task(session, conv_id=1, user_id=1)
        await _seed_summary(session, conv_id=1, user_id=1)
        await session.commit()

    async with sqlite_session_factory() as session:
        svc = ConversationContextService(session)
        # Mirror MessageService.send_message: intent first, then chat.
        # Note: build_intent_context is called first; build_chat_context
        # then piggybacks on the cached load.
        with _TableSelectCounter(session.get_bind()) as c:
            await svc.build_intent_context(
                user_id=1, conversation_id=1, current_message_id=None, max_turns=3
            )
            await svc.build_chat_context(
                user_id=1, conversation_id=1, current_message_id=None, max_turns=10
            )

    # Aggregate per-table SELECT count must stay flat for the four
    # cross-builder tables.
    cross_builder = ("messages", "conversation_summaries",
                     "uploaded_files", "agent_tasks")
    for tbl in cross_builder:
        assert c.selects(tbl) == 1, (
            f"{tbl} should be queried exactly once in the intent+chat "
            f"path; got {c.selects(tbl)}.  all selects_by_table="
            f"{c.selects_by_table}"
        )


@pytest.mark.asyncio
async def test_independent_service_instance_rereads(
    sqlite_session_factory,
):
    """Sanity check: a *new* ConversationContextService instance must
    re-query (the dedupe is process-local, not cross-request).  This
    matches how ``MessageService`` instantiates a fresh service per
    request.
    """
    async with sqlite_session_factory() as session:
        await _seed_user(session)
        await _seed_conversation(session, conv_id=1, user_id=1)
        await _seed_messages(session, conv_id=1, user_id=1, count=2)
        await session.commit()

    async with sqlite_session_factory() as session:
        svc_a = ConversationContextService(session)
        with _TableSelectCounter(session.get_bind()) as c_a:
            await svc_a.build_intent_context(
                user_id=1, conversation_id=1, current_message_id=None, max_turns=3
            )
        first_pass = c_a.selects("messages")

    async with sqlite_session_factory() as session:
        svc_b = ConversationContextService(session)
        with _TableSelectCounter(session.get_bind()) as c_b:
            await svc_b.build_intent_context(
                user_id=1, conversation_id=1, current_message_id=None, max_turns=3
            )
        second_pass = c_b.selects("messages")

    assert first_pass == 1
    assert second_pass == 1, (
        "A fresh service instance must re-read so cross-request "
        "lifecycle is correct; got %d" % second_pass
    )
