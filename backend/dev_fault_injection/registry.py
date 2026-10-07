"""Fault scenario registry."""

from __future__ import annotations

from typing import Protocol

from app.fault_injection_gateway import (
    POINT_AFTER_TEST_PLAN_LLM_RAW_JSON,
    POINT_AFTER_WORD_EXPORT_TEMPLATE_RENDER,
)


class FaultScenario(Protocol):
    key: str
    point: str

    def apply(self, payload, context: dict):
        ...


def _build_scenarios() -> tuple[FaultScenario, ...]:
    from .scenarios.format_loss import BookmarkFormatLossScenario
    from .scenarios.json_format import (
        EmptySectionContentScenario,
        ExtraTableWithoutPlaceholderScenario,
        JsonTruncateScenario,
        MissingSectionScenario,
        MissingTableForPlaceholderScenario,
        MultipleTablesForSinglePlaceholderScenario,
        SchemaHeaderMismatchScenario,
        SectionInvalidValueScenario,
        TableRowNotObjectScenario,
    )

    return (
        SchemaHeaderMismatchScenario(),
        MissingSectionScenario(),
        JsonTruncateScenario(),
        EmptySectionContentScenario(),
        SectionInvalidValueScenario(),
        ExtraTableWithoutPlaceholderScenario(),
        MultipleTablesForSinglePlaceholderScenario(),
        MissingTableForPlaceholderScenario(),
        TableRowNotObjectScenario(),
        BookmarkFormatLossScenario(),
    )


_SCENARIOS = _build_scenarios()


def scenarios_for(point: str) -> tuple[FaultScenario, ...]:
    return tuple(scenario for scenario in _SCENARIOS if scenario.point == point)


__all__ = [
    "POINT_AFTER_TEST_PLAN_LLM_RAW_JSON",
    "POINT_AFTER_WORD_EXPORT_TEMPLATE_RENDER",
    "scenarios_for",
]
