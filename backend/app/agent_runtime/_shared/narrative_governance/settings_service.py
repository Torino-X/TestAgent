"""Phase 2.9C narrative settings.

Developer-only verbosity is resolved from
``AGENT_RUNTIME_NARRATIVE_DETAIL_LEVEL``. User settings only control whether
tool-call cards render the public narrative block below the card. Dynamic
Agent narrative events are not affected by this user-facing switch.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_runtime._shared.narrative_governance.schemas import (
    NarrativeDetailLevel,
)
from app.core.config import get_settings
from app.models.config import SystemConfig
from app.utils.datetime import utcnow


logger = logging.getLogger(__name__)

_USER_ENABLED_KEY_PREFIX = "tool_card_narrative_enabled"
_LEGACY_USER_LEVEL_KEY_PREFIX = "narrative_detail_level"


@dataclass(frozen=True)
class NarrativeUserSettings:
    enabled: bool
    detail_level: NarrativeDetailLevel
    detail_level_source: str = "env"


def _parse_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    logger.warning(
        "Invalid tool-card narrative enabled value, using default | value=%r default=%s",
        value,
        default,
    )
    return default


def resolve_env_detail_level() -> NarrativeDetailLevel:
    raw = getattr(get_settings(), "agent_runtime_narrative_detail_level", "standard")
    parsed = NarrativeDetailLevel.parse(raw)
    if str(raw).strip().lower() != parsed.value:
        logger.warning(
            "Invalid AGENT_RUNTIME_NARRATIVE_DETAIL_LEVEL, falling back to standard | value=%r",
            raw,
        )
    return parsed


class NarrativeSettingsService:
    """Per-user tool-card narrative visibility persistence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _enabled_key(user_internal_id: int) -> str:
        return f"{_USER_ENABLED_KEY_PREFIX}::{int(user_internal_id)}"

    @staticmethod
    def _legacy_user_level_key(user_internal_id: int) -> str:
        return f"{_LEGACY_USER_LEVEL_KEY_PREFIX}::{int(user_internal_id)}"

    async def get_user_settings(self, user_internal_id: int) -> NarrativeUserSettings:
        return NarrativeUserSettings(
            enabled=await self.get_user_enabled(user_internal_id),
            detail_level=resolve_env_detail_level(),
        )

    async def get_user_enabled(self, user_internal_id: int) -> bool:
        result = await self._session.execute(
            select(SystemConfig).where(
                SystemConfig.config_key == self._enabled_key(user_internal_id)
            )
        )
        row = result.scalar_one_or_none()
        if row and row.config_value:
            return _parse_bool(row.config_value, default=True)
        return True

    async def set_user_enabled(
        self,
        user_internal_id: int,
        enabled: bool,
    ) -> NarrativeUserSettings:
        now = utcnow()
        key = self._enabled_key(user_internal_id)
        result = await self._session.execute(
            select(SystemConfig).where(SystemConfig.config_key == key)
        )
        row = result.scalar_one_or_none()
        value = "true" if enabled else "false"
        if row is not None:
            row.config_value = value
            row.updated_at = now
        else:
            self._session.add(
                SystemConfig(
                    config_key=key,
                    config_value=value,
                    value_type="boolean",
                    description=(
                        "Per-user tool-call card narrative visibility (Phase 2.9C)."
                    ),
                    editable=True,
                    created_at=now,
                    updated_at=now,
                )
            )
        await self._session.flush()
        return await self.get_user_settings(user_internal_id)

    async def get_user_level(self, user_internal_id: int) -> NarrativeDetailLevel:
        return await self.get_user_level_dict(user_internal_id)

    async def get_user_level_dict(self, user_internal_id: int) -> NarrativeDetailLevel:
        return resolve_env_detail_level()

    async def set_user_level(
        self,
        user_internal_id: int,
        level: NarrativeDetailLevel | str,
    ) -> NarrativeDetailLevel:
        effective = resolve_env_detail_level()
        logger.warning(
            "Ignoring legacy per-user narrative detail write; use "
            "AGENT_RUNTIME_NARRATIVE_DETAIL_LEVEL | user_internal_id=%s "
            "requested=%r effective=%s",
            user_internal_id,
            level,
            effective.value,
        )
        return effective

    async def clear_user_level(self, user_internal_id: int) -> None:
        result = await self._session.execute(
            select(SystemConfig).where(
                SystemConfig.config_key == self._legacy_user_level_key(user_internal_id)
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            await self._session.delete(row)
            await self._session.flush()


async def resolve_detail_level(
    session: AsyncSession,
    user_internal_id: int | None,
) -> NarrativeDetailLevel:
    """Resolve the developer-configured effective detail level."""
    return resolve_env_detail_level()


async def is_tool_card_narrative_generation_enabled(ctx: object) -> bool:
    """Return whether this user's tool-card narration may call an LLM.

    The setting used to be treated as a front-end-only visibility preference.
    That left every tool invocation paying for a narrative completion even
    when the card deliberately hid the result.  Runtime contexts created in
    production expose a session-scoped settings service; read the persisted
    user preference before a composer is constructed.

    A missing settings service is retained as an enabled compatibility path
    for isolated legacy/unit runtime contexts.  A real resolver failure is
    fail-closed: it must not create an unrequested LLM cost.
    """
    settings_service = getattr(ctx, "settings_service", None)
    if settings_service is None:
        return True

    resolver = getattr(settings_service, "tool_card_narrative_enabled", None)
    if not callable(resolver):
        logger.warning(
            "Tool-card narrative preference resolver missing; skipping LLM narration | user=%s",
            getattr(ctx, "user_internal_id", None),
        )
        return False

    try:
        enabled = resolver()
        if inspect.isawaitable(enabled):
            enabled = await enabled
        return bool(enabled)
    except Exception as exc:  # noqa: BLE001 - never allow a preference read to fail a task
        logger.warning(
            "Tool-card narrative preference resolution failed; skipping LLM narration | user=%s | error=%s",
            getattr(ctx, "user_internal_id", None),
            type(exc).__name__,
        )
        return False


__all__ = [
    "NarrativeSettingsService",
    "NarrativeUserSettings",
    "is_tool_card_narrative_generation_enabled",
    "resolve_detail_level",
    "resolve_env_detail_level",
]
# narrative_governance.settings_service:开发者级 verbosity 解析(AGENT_RUNTIME_NARRATIVE_DETAIL_LEVEL);与用户设置解耦。
