from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.conversations import (
    _ContextSettingsRequest,
    get_conversation_context_settings,
    update_conversation_context_settings,
)
from app.models.conversation import Conversation


class _ScalarResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self.row = row
        self.execute_calls = 0
        self.commit_calls = 0

    async def execute(self, _statement):
        self.execute_calls += 1
        return _ScalarResult(self.row)

    async def commit(self):
        self.commit_calls += 1


def _user():
    return SimpleNamespace(internal_id=1)


def test_conversation_model_has_server_knowledge_mode_column() -> None:
    assert hasattr(Conversation, "knowledge_mode")


@pytest.mark.asyncio
async def test_context_settings_get_reads_persisted_knowledge_mode() -> None:
    row = SimpleNamespace(
        context_memory_mode="inherit",
        knowledge_mode="MAAS_STRICT",
        context_workspace_key="conversation:conv_001",
        context_engine_version="v3",
        deleted_at=None,
    )
    response = await get_conversation_context_settings(
        conversation_id="conv_001",
        current_user=_user(),
        session=_FakeSession(row),
    )

    assert response["data"]["knowledge_mode"] == "MAAS_STRICT"
    assert response["data"]["knowledge_mode_persistence"] == "server"
    assert response["data"]["knowledge_mode_migration_required"] is False


@pytest.mark.asyncio
async def test_context_settings_patch_persists_knowledge_mode() -> None:
    row = SimpleNamespace(
        context_memory_mode="inherit",
        knowledge_mode="AUTO",
        context_workspace_key="conversation:conv_001",
        context_engine_version="v3",
        deleted_at=None,
    )
    session = _FakeSession(row)

    response = await update_conversation_context_settings(
        body=_ContextSettingsRequest(knowledge_mode="MAAS_STRICT"),
        conversation_id="conv_001",
        current_user=_user(),
        session=session,
    )

    assert row.knowledge_mode == "MAAS_STRICT"
    assert session.commit_calls == 1
    assert response["data"]["knowledge_mode"] == "MAAS_STRICT"
    assert response["data"]["knowledge_mode_persistence"] == "server"
    assert response["data"]["knowledge_mode_migration_required"] is False


@pytest.mark.asyncio
async def test_context_settings_get_cross_user_not_found_uses_numeric_404() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await get_conversation_context_settings(
            conversation_id="conv_other_user",
            current_user=_user(),
            session=_FakeSession(None),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == 40401


@pytest.mark.asyncio
async def test_context_settings_patch_cross_user_not_found_uses_numeric_404() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await update_conversation_context_settings(
            body=_ContextSettingsRequest(knowledge_mode="MAAS_STRICT"),
            conversation_id="conv_other_user",
            current_user=_user(),
            session=_FakeSession(None),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == 40401
