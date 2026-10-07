from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.message import Message
from app.repositories.message_repository import MessageRepository


class _ScalarResult:
    def __init__(self, value: int):
        self.value = value

    def scalar(self) -> int:
        return self.value


class _InsertResult:
    def __init__(self, lastrowid: int):
        self.lastrowid = lastrowid


class _NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _SequenceConflictSession:
    def __init__(self):
        self.sequence_values = [18, 19]
        self.lock_calls = 0
        self.insert_calls = 0
        self.flush_calls = 0
        self.rollback_calls = 0

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

    async def execute(self, statement, _params=None):
        sql = str(statement)
        if "FROM conversations" in sql and "FOR UPDATE" in sql:
            self.lock_calls += 1
            return _ScalarResult(1)
        if "FROM messages" in sql and "FOR UPDATE" in sql:
            return _ScalarResult(self.sequence_values.pop(0))
        if "INSERT INTO messages" in sql:
            self.insert_calls += 1
            if self.insert_calls == 1:
                raise IntegrityError(
                    statement,
                    _params,
                    Exception(
                        "Duplicate entry '191-19' for key "
                        "'messages.uq_messages_conversation_sequence'"
                    ),
                )
            return _InsertResult(321)
        raise AssertionError(f"unexpected SQL: {sql}")

    async def flush(self):
        self.flush_calls += 1

    async def rollback(self):
        self.rollback_calls += 1

    def begin_nested(self):
        return _NestedTransaction()


@pytest.mark.asyncio
async def test_create_retries_allocated_sequence_conflict(caplog):
    session = _SequenceConflictSession()
    repo = MessageRepository(session)  # type: ignore[arg-type]
    now = datetime(2026, 8, 13, 12, 53, 59, tzinfo=timezone.utc)
    message = Message(
        public_id="msg_retry",
        user_id=1,
        conversation_id=191,
        task_id=None,
        role="user",
        message_type="user_text",
        content="why idempotency?",
        payload_json={"attached_file_ids": []},
        status="sent",
        created_at=now,
        updated_at=now,
    )

    created = await repo.create(message)

    assert created.id == 321
    assert created.conversation_sequence == 20
    assert session.lock_calls == 2
    assert session.insert_calls == 2
    assert session.flush_calls == 1
    assert session.rollback_calls == 0
    assert "FALLBACK_USED | component=message_repository" in caplog.text
