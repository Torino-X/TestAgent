"""Phase 2.9C emitter-side adapter.

The emitter is the single boundary through which both Phase 2.9A
(deterministic builder) and Phase 2.9B (dynamic envelope) reach
the SSE pipeline. In Phase 2.9C we attach the
:class:`NarrativeGovernanceService` here so candidates pass through
the same governance gate, but only when the master flag
``phase29c_governance_enabled`` is on. When off, callers see the
2.9A / 2.9B baseline unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from app.agent_runtime._shared.narrative_governance.schemas import (
    NarrativeGovernanceContext,
    NarrativeGovernanceResult,
    NarrativeRepetitionState,
)
from app.agent_runtime._shared.narrative_governance.service import (
    NarrativeGovernanceService,
)
from app.agent_runtime import feature_flags as _ff_module


logger = logging.getLogger(__name__)

# Sentinel — instantiated lazily so importing this module is cheap.
_SERVICE: NarrativeGovernanceService | None = None


def _service() -> NarrativeGovernanceService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = NarrativeGovernanceService()
    return _SERVICE


# Module-level indirection so tests can ``monkeypatch.setattr`` the
# attribute without re-binding the import inside this module.
DetailLevelResolver = Callable[
    [Any, int | None],
    Awaitable[Any],
]
_async_resolve_detail_level: DetailLevelResolver | None = None


def register_detail_level_resolver(
    resolver: DetailLevelResolver,
) -> None:
    """Register an async resolver used to fetch per-user level.

    Tests may inject a fake here; production code wires
    ``narrative_governance.settings_service.resolve_detail_level``.
    """
    global _async_resolve_detail_level
    _async_resolve_detail_level = resolver


def is_governance_active() -> bool:
    """Return whether the master Phase 2.9C switch is on.

    Always looks up ``get_feature_flags`` on the
    ``app.agent_runtime.feature_flags`` module — this lets tests
    monkey-patch that attribute and have the change reflected here.
    """
    get_feature_flags = _ff_module.get_feature_flags
    return bool(
        getattr(get_feature_flags(), "phase29c_governance_enabled", False)
    )


async def govern_async_payload(
    *,
    candidate: dict[str, Any] | None,
    context: NarrativeGovernanceContext,
    session: Any = None,
    user_internal_id: int | None = None,
    repetition: NarrativeRepetitionState | None = None,
) -> dict[str, Any] | None:
    """Govern a public-update payload through the unified pipeline.

    Behaviour:

    * Master switch off → candidate passes through unchanged.
    * Master switch on → detail-level projection + sanitizer +
      fact / route / scope checks + semantic dedup + deterministic
      compression run; a result with ``action == suppress`` returns
      ``None``.

    The function NEVER raises — exceptions are logged and the
    caller receives the original candidate so the agent main loop
    is unaffected.
    """
    if not is_governance_active():
        return candidate if isinstance(candidate, dict) else None

    if (
        session is not None
        and user_internal_id is not None
        and _async_resolve_detail_level is not None
    ):
        try:
            level = await _async_resolve_detail_level(session, user_internal_id)
            context = context.model_copy(update={"detail_level": level})
        except Exception as exc:  # noqa: BLE001
            logger.debug("governance: detail-level resolve failed: %s", exc)

    try:
        svc = _service()
        result: NarrativeGovernanceResult = svc.govern(
            candidate=candidate,
            context=context,
            repetition=repetition,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("narrative governance call failed: %s", exc)
        return candidate if isinstance(candidate, dict) else None

    if result.action == "suppress":
        return None
    if result.action == "fallback" and result.public_update is None:
        return candidate if isinstance(candidate, dict) else None
    return result.public_update


# Late import + bind so test ``monkeypatch.setattr(emitter_adapter,
# resolve_detail_level, ...)`` works without re-import gymnastics.
from app.agent_runtime._shared.narrative_governance.settings_service import (  # noqa: E402
    resolve_detail_level as _resolve_detail_level,
)


register_detail_level_resolver(_resolve_detail_level)


__all__ = [
    "govern_async_payload",
    "is_governance_active",
    "register_detail_level_resolver",
]
# narrative_governance.emitter_adapter:2.9A/2.9B 输出统一入口;按 phase29c_governance_enabled 决定是否走治理关卡。
