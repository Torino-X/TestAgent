"""F020 — repository test for image_understanding_configs upsert behaviour.

We exercise the unit-level methods against a synchronous-mocked
session; the goal is to prove that ``upsert_for_user`` preserves the
existing ciphertext when the caller doesn't supply a fresh key, and
that ``update_test_status`` mutates the most recent row only.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.repositories.image_understanding_config_repository import (
    ImageUnderstandingConfigRepository,
)


def _row(existing_enc: str = "ENC-OLD", masked: str = "old-****"):
    return SimpleNamespace(
        api_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_encrypted=existing_enc,
        api_key_masked=masked,
        model_name="qwen-vl-plus",
        timeout_seconds=60,
        max_tokens=None,
        enable_in_doc_parsing=False,
        last_test_status=None,
        last_test_message=None,
        last_test_at=None,
        updated_at=datetime(2026, 6, 30, 12, 0, 0),
    )


@pytest.mark.asyncio
async def test_upsert_inserts_when_no_existing_row():
    session = MagicMock()
    session.execute = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    # First call: get_for_user lookup → None
    # All later calls inside the same flow
    session.execute.side_effect = [
        _async_result(None),
        _async_result(None),
    ]

    repo = ImageUnderstandingConfigRepository(session)
    new_row = await repo.upsert_for_user(
        user_id=42,
        public_id=None,
        api_base_url="https://example.com/v1",
        api_key_encrypted="ENC-FRESH",
        api_key_masked="abc-****wxyz",
        model_name="qwen-vl-plus",
        timeout_seconds=60,
        max_tokens=2048,
        enable_in_doc_parsing=True,
    )

    assert session.add.called
    session.flush.assert_awaited()
    assert new_row.api_key_encrypted == "ENC-FRESH"
    assert new_row.api_key_masked == "abc-****wxyz"
    assert new_row.enable_in_doc_parsing is True
    assert new_row.max_tokens == 2048


@pytest.mark.asyncio
async def test_upsert_preserves_existing_key_when_none_supplied():
    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    existing = _row()
    # Two execute calls: get_for_user + the upsert's existing lookup
    session.execute.side_effect = [
        _async_result(existing),
        _async_result(existing),
    ]

    repo = ImageUnderstandingConfigRepository(session)
    updated = await repo.upsert_for_user(
        user_id=42,
        public_id="iuc_001",
        api_base_url="https://example.com/v1",
        api_key_encrypted="",  # masked blank — must be ignored
        api_key_masked=None,
        model_name="qwen-vl-max",
        timeout_seconds=90,
        max_tokens=4096,
        enable_in_doc_parsing=True,
    )

    # Model and switch changed; key and mask untouched.
    assert updated is existing
    assert existing.api_key_encrypted == "ENC-OLD"
    assert existing.api_key_masked == "old-****"
    assert existing.model_name == "qwen-vl-max"
    assert existing.timeout_seconds == 90
    assert existing.max_tokens == 4096
    assert existing.enable_in_doc_parsing is True


@pytest.mark.asyncio
async def test_upsert_overwrites_key_when_new_value_supplied():
    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    existing = _row()
    session.execute.side_effect = [_async_result(existing), _async_result(existing)]

    repo = ImageUnderstandingConfigRepository(session)
    await repo.upsert_for_user(
        user_id=42,
        public_id="iuc_001",
        api_base_url="https://example.com/v1",
        api_key_encrypted="ENC-NEW",
        api_key_masked="zzz-****wxyz",
        model_name="qwen-vl-plus",
        timeout_seconds=60,
        max_tokens=None,
        enable_in_doc_parsing=False,
    )

    assert existing.api_key_encrypted == "ENC-NEW"
    assert existing.api_key_masked == "zzz-****wxyz"


@pytest.mark.asyncio
async def test_update_test_status_only_mutates_most_recent_row():
    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    target = SimpleNamespace(last_test_status=None, last_test_message=None, last_test_at=None)
    session.execute.side_effect = [
        _async_result(target),  # first query inside update_test_status
    ]

    repo = ImageUnderstandingConfigRepository(session)
    await repo.update_test_status(user_id=42, status="success", message="ok")

    assert target.last_test_status == "success"
    assert target.last_test_message == "ok"
    assert target.last_test_at is not None


class _AsyncResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _async_result(value):
    return _AsyncResult(value)
