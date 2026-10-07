"""Phase 2.9C settings service unit tests (no DB)."""

from __future__ import annotations

from typing import Any

import pytest

from app.agent_runtime._shared.narrative_governance.schemas import (
    NarrativeDetailLevel,
)
from app.agent_runtime._shared.narrative_governance.settings_service import (
    NarrativeSettingsService,
    is_tool_card_narrative_generation_enabled,
    resolve_detail_level,
)
from app.core.config import get_settings


class _StubSession:
    """In-memory stand-in for ``AsyncSession`` used by ``SystemConfig``."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self._flushes = 0

    async def execute(self, stmt):  # noqa: D401
        try:
            key = stmt._whereclause.right.value  # type: ignore[attr-defined]
        except AttributeError:
            key = None
        return _AsyncResult(self._store.get(key))

    async def flush(self) -> None:
        self._flushes += 1

    async def delete(self, row) -> None:
        if hasattr(row, "config_key") and row.config_key in self._store:
            self._store.pop(row.config_key, None)

    def add(self, instance) -> None:
        self._store[instance.config_key] = instance


class _AsyncResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.mark.asyncio
async def test_default_detail_level_is_standard_when_env_unset(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_NARRATIVE_DETAIL_LEVEL", raising=False)
    get_settings.cache_clear()
    session = _StubSession()

    level = await resolve_detail_level(session, user_internal_id=9999999)

    assert level is NarrativeDetailLevel.STANDARD
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_detail_level_comes_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_NARRATIVE_DETAIL_LEVEL", "detailed")
    get_settings.cache_clear()
    session = _StubSession()
    svc = NarrativeSettingsService(session)

    assert await svc.get_user_level_dict(987654323) is NarrativeDetailLevel.DETAILED
    settings = await svc.get_user_settings(987654323)
    assert settings.detail_level is NarrativeDetailLevel.DETAILED
    assert settings.detail_level_source == "env"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_set_and_get_user_enabled_round_trip():
    session = _StubSession()
    svc = NarrativeSettingsService(session)

    persisted = await svc.set_user_enabled(987654321, False)

    assert persisted.enabled is False
    assert await svc.get_user_enabled(987654321) is False


@pytest.mark.asyncio
async def test_invalid_user_enabled_value_falls_back_to_enabled():
    session = _StubSession()
    session._store["tool_card_narrative_enabled::987654322"] = type(
        "Row", (), {"config_value": "not-a-bool"}
    )()
    svc = NarrativeSettingsService(session)

    assert await svc.get_user_enabled(987654322) is True


@pytest.mark.asyncio
async def test_set_user_enabled_persists_when_flush_invoked():
    session = _StubSession()
    svc = NarrativeSettingsService(session)

    await svc.set_user_enabled(987654324, True)

    assert session._flushes == 1


@pytest.mark.asyncio
async def test_runtime_tool_card_narrative_setting_is_read_before_generation():
    class _Settings:
        async def tool_card_narrative_enabled(self):
            return False

    ctx = type("Context", (), {"settings_service": _Settings(), "user_internal_id": 7})()

    assert await is_tool_card_narrative_generation_enabled(ctx) is False


@pytest.mark.asyncio
async def test_runtime_tool_card_narrative_setting_fails_closed_on_resolver_error():
    class _Settings:
        async def tool_card_narrative_enabled(self):
            raise RuntimeError("database unavailable")

    ctx = type("Context", (), {"settings_service": _Settings(), "user_internal_id": 7})()

    assert await is_tool_card_narrative_generation_enabled(ctx) is False
