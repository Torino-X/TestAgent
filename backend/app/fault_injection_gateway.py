"""Production-safe gateway for optional development fault injection.

Business code may call this module, but the actual fault implementation lives
outside ``app`` in ``dev_fault_injection``. If that package is absent, disabled,
or broken, the gateway returns the original payload unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

POINT_AFTER_TEST_PLAN_LLM_RAW_JSON = "after_test_plan_llm_raw_json"
POINT_AFTER_WORD_EXPORT_TEMPLATE_RENDER = "after_word_export_template_render"


def maybe_inject_fault(
    point: str,
    payload: Any,
    context: dict[str, Any] | None = None,
) -> Any:
    """Optionally inject a dev-only fault at ``point``.

    The gateway intentionally swallows import/runtime failures so production
    code never depends on the optional dev fault package.
    """

    try:
        from dev_fault_injection.engine import maybe_inject
    except Exception:
        return payload

    try:
        return maybe_inject(point=point, payload=payload, context=context or {})
    except Exception:
        logger.warning(
            "FaultInjectionGateway: injection failed | point=%s | fallback=noop",
            point,
            exc_info=True,
        )
        return payload
