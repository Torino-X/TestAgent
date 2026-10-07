"""Dev fault injection execution engine."""

from __future__ import annotations

import logging
from typing import Any

from .registry import scenarios_for
from .settings import FaultInjectionSettings

logger = logging.getLogger(__name__)


def maybe_inject(point: str, payload: Any, context: dict[str, Any] | None = None) -> Any:
    ctx = context or {}
    settings = FaultInjectionSettings.from_env()
    if not settings.enabled_for(ctx):
        return payload

    enabled_scenarios = set(settings.scenarios)
    if not enabled_scenarios:
        return payload

    available_scenarios = tuple(scenarios_for(point))
    for scenario in available_scenarios:
        if scenario.key not in enabled_scenarios:
            continue
        logger.warning(
            "FAULT_INJECTION_TRIGGERED | point=%s | scenario=%s | task_id=%s",
            point,
            scenario.key,
            ctx.get("task_id"),
        )
        try:
            injected_payload = scenario.apply(payload, ctx)
            logger.warning(
                "FAULT_INJECTION_APPLIED | point=%s | scenario=%s | task_id=%s",
                point,
                scenario.key,
                ctx.get("task_id"),
            )
            return injected_payload
        except Exception:
            logger.warning(
                "FAULT_INJECTION_FAILED | point=%s | scenario=%s | fallback=noop",
                point,
                scenario.key,
                exc_info=True,
            )
            return payload

    logger.warning(
        "FAULT_INJECTION_NO_MATCH | point=%s | enabled_scenarios=%s | available_scenarios=%s | task_id=%s",
        point,
        sorted(enabled_scenarios),
        [scenario.key for scenario in available_scenarios],
        ctx.get("task_id"),
    )
    return payload
