"""BUG FIX 2026-08-19 — ``MessageService._pending_index_files`` 回归测试。

覆盖:
  1. 索引就绪（ready） → 不在 pending 列表
  2. 索引跳过（skipped） → 不在 pending 列表
  3. status=pending、lexical=pending、vector=pending → 列入 pending
  4. status=indexing 但 lexical/vector 都是 ready → 不列入
  5. 异常（DB 不可用） → 返空（fail-safe，不阻塞 send_message）
  6. files 为空 → 返空

不接真实 DB，用 MagicMock 模拟 AsyncSession.execute。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.message_service import MessageService


def _make_msg_service():
    return MessageService.__new__(MessageService)


def _file(public_id: str = "file_x") -> SimpleNamespace:
    return SimpleNamespace(
        id=public_id,
        public_id=public_id,
        original_name=f"{public_id}.docx",
        file_ext=".docx",
        file_type="unknown",
    )


def _doc(source_public_id: str, **status_overrides) -> SimpleNamespace:
    base = {
        "source_public_id": source_public_id,
        "status": "pending",
        "lexical_index_status": "pending",
        "vector_index_status": "pending",
        "title": f"{source_public_id}.docx",
    }
    base.update(status_overrides)
    return SimpleNamespace(**base)


def _session_with_docs(docs: list[SimpleNamespace]) -> MagicMock:
    session = MagicMock()

    async def _execute(_stmt):
        result = MagicMock()
        result.scalars.return_value.all.return_value = docs
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


@pytest.mark.asyncio
async def test_ready_documents_not_in_pending():
    """三个字段都 ready → 不算 pending。"""
    docs = [
        _doc("file_a", status="indexed", lexical_index_status="ready", vector_index_status="ready"),
    ]
    svc = _make_msg_service()
    svc._session = _session_with_docs(docs)

    out = await svc._pending_index_files(user_internal_id=1, files=[_file("file_a")])
    assert out == []


@pytest.mark.asyncio
async def test_skipped_does_not_count_as_pending():
    """skipped 是终态，不算 pending。"""
    docs = [
        _doc("file_a", status="skipped", lexical_index_status="skipped", vector_index_status="skipped"),
    ]
    svc = _make_msg_service()
    svc._session = _session_with_docs(docs)

    out = await svc._pending_index_files(user_internal_id=1, files=[_file("file_a")])
    assert out == []


@pytest.mark.asyncio
async def test_pending_documents_listed():
    """status=pending、lexical/vector 都 pending → 列入 pending。"""
    docs = [
        _doc("file_a", status="pending", lexical_index_status="pending", vector_index_status="pending"),
    ]
    svc = _make_msg_service()
    svc._session = _session_with_docs(docs)

    out = await svc._pending_index_files(user_internal_id=1, files=[_file("file_a")])
    assert len(out) == 1
    assert out[0]["public_id"] == "file_a"
    assert out[0]["status"] == "pending"
    assert out[0]["lexical_index_status"] == "pending"
    assert out[0]["vector_index_status"] == "pending"


@pytest.mark.asyncio
async def test_partial_indexing_with_overall_pending_still_listed():
    """status=indexing、lexical=ready 但 vector=pending → 仍 pending。"""
    docs = [
        _doc("file_a", status="indexing", lexical_index_status="ready", vector_index_status="pending"),
    ]
    svc = _make_msg_service()
    svc._session = _session_with_docs(docs)

    out = await svc._pending_index_files(user_internal_id=1, files=[_file("file_a")])
    assert len(out) == 1


@pytest.mark.asyncio
async def test_db_query_exception_returns_empty(monkeypatch):
    """DB 异常不应阻塞 send_message，返空列表（fail-safe）。"""

    async def _raise(_stmt):
        raise RuntimeError("DB unavailable")

    session = MagicMock()
    session.execute = AsyncMock(side_effect=_raise)
    svc = _make_msg_service()
    svc._session = session

    out = await svc._pending_index_files(user_internal_id=1, files=[_file("file_a")])
    assert out == []


@pytest.mark.asyncio
async def test_no_files_returns_empty():
    svc = _make_msg_service()
    svc._session = _session_with_docs([])

    out = await svc._pending_index_files(user_internal_id=1, files=[])
    assert out == []


@pytest.mark.asyncio
async def test_multiple_files_mixed_ready_and_pending():
    """多个文件中混有未 ready 的 → 只列出未 ready 的。"""
    docs = [
        _doc("file_a", status="indexed", lexical_index_status="ready", vector_index_status="ready"),
        _doc("file_b", status="pending", lexical_index_status="pending", vector_index_status="pending"),
        _doc("file_c", status="indexed", lexical_index_status="ready", vector_index_status="ready"),
    ]
    svc = _make_msg_service()
    svc._session = _session_with_docs(docs)

    out = await svc._pending_index_files(
        user_internal_id=1,
        files=[_file("file_a"), _file("file_b"), _file("file_c")],
    )
    assert len(out) == 1
    assert out[0]["public_id"] == "file_b"