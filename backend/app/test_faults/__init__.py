"""Backward-compatible wrappers for dev fault injection.

The concrete fault scenarios live in ``backend/dev_fault_injection`` so they can
be removed as one unit before release. New business code should import only
``app.fault_injection_gateway``.
"""

from __future__ import annotations

from typing import Any

from app.fault_injection_gateway import (
    POINT_AFTER_TEST_PLAN_LLM_RAW_JSON,
    POINT_AFTER_WORD_EXPORT_TEMPLATE_RENDER,
    maybe_inject_fault,
)


def apply_test_faults(
    raw_json: str,
    generation_config: dict[str, Any] | None = None,
) -> str:
    return maybe_inject_fault(
        POINT_AFTER_TEST_PLAN_LLM_RAW_JSON,
        raw_json,
        context={"generation_config": generation_config or {}},
    )


def apply_format_loss_faults(exporter: Any) -> None:
    maybe_inject_fault(
        POINT_AFTER_WORD_EXPORT_TEMPLATE_RENDER,
        exporter,
        context={},
    )
