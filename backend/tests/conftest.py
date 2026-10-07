"""Phase 2.9A.37 — shared real-DB test infrastructure.

Windows 下默认 ``ProactorEventLoop`` 无法运行 psycopg 异步模式;生产 Postgres
Checkpointer(``AsyncPostgresSaver``)需要 ``SelectorEventLoop``。在收集期全局切到
``WindowsSelectorEventLoopPolicy``,使所有 async 测试(含持久化 checkpoint 测试)
都在 Selector 下运行。非 Windows 平台不受影响。

Provides an in-memory SQLite engine so repository / service integration
tests exercise the *real* SQL that production runs, including ordering and
LIMIT semantics.

Why SQLite reproduces the production bug faithfully:
  * MySQL's ``created_at`` column is ``DATETIME`` (second precision).
    When two rows share the same second, ``ORDER BY created_at DESC``
    without a secondary key is a filesort tie — MySQL returns the row
    with the *smaller* id in practice.  SQLite does exactly the same
    (rows come back in insertion order for equal sort keys).
  * The regeneration latest-check therefore selects the user message
    (id 149) instead of the agent reply (id 150) when both were written
    within the same second by ``MessageService.send_message``.

Only ``assistant_message_generations`` is recreated with an
``INTEGER PRIMARY KEY``: the production repository inserts generation
rows via raw SQL that omits ``id``, and SQLite only auto-increments a
plain ``INTEGER PRIMARY KEY`` column (not ``BIGINT``).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

if sys.platform == "win32" and not isinstance(
    asyncio.get_event_loop_policy(), asyncio.WindowsSelectorEventLoopPolicy
):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — register every model on Base
from app.db.base import Base

_GENERATION_TABLE_DDL = """
CREATE TABLE assistant_message_generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id VARCHAR(64) NOT NULL UNIQUE,
    message_id BIGINT NOT NULL,
    generation_no INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    content_markdown TEXT,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    error_code VARCHAR(32),
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""

# The messages table is recreated WITHOUT the production
# ``uq_messages_conversation_sequence`` unique constraint so tests can
# exercise the `id DESC` tiebreaker against deliberately corrupt data
# (duplicate conversation_sequence) — the scenario the stable-ordering
# contract must survive.  Column set mirrors app/models/message.py.
_MESSAGES_TABLE_DDL = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id VARCHAR(64) NOT NULL UNIQUE,
    user_id BIGINT NOT NULL,
    conversation_id BIGINT NOT NULL,
    task_id BIGINT,
    role VARCHAR(32) NOT NULL,
    message_type VARCHAR(64) NOT NULL,
    content TEXT,
    payload_json TEXT,
    status VARCHAR(32) DEFAULT 'sent',
    conversation_sequence BIGINT,
    reply_to_message_id BIGINT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    deleted_at DATETIME
)
"""


def _sql_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
async def sqlite_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _register_now(dbapi_connection, _connection_record):
        dbapi_connection.create_function("NOW", 0, _sql_now)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DROP TABLE assistant_message_generations"))
        await conn.execute(text(_GENERATION_TABLE_DDL))
        await conn.execute(text("DROP TABLE messages"))
        await conn.execute(text(_MESSAGES_TABLE_DDL))

    yield engine
    await engine.dispose()


@pytest.fixture()
async def sqlite_session_factory(sqlite_engine):
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)
