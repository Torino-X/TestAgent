"""Environment parsing for dev fault injection."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

TRUTHY = {"1", "true", "yes", "on"}
PROD_ENVS = {"prod", "production"}
DEFAULT_ALLOWED_ENVS = frozenset({"development", "dev", "local", "test"})

LEGACY_SCENARIO_ENV = (
    ("FAULT_SCHEMA_HEADER_MISMATCH", "result_review.schema_header_mismatch"),
    ("FAULT_MISSING_SECTION", "result_review.missing_section"),
    ("FAULT_JSON_TRUNCATE", "result_review.json_truncate"),
    ("FAULT_EMPTY_SECTION_CONTENT", "result_review.empty_section_content"),
    ("FAULT_SECTION_INVALID_VALUE", "result_review.section_invalid_value"),
    (
        "FAULT_FORMAT_LOSS_BOOKMARK_SIMULATE",
        "format_loss.bookmark_simulate",
    ),
)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY


def _normalize_csv_value(value: str) -> str:
    return value.strip().strip('"').strip("'")


def _normalize_scenario_id(value: str) -> str:
    # Users often copy scenario ids from escaped markdown/env examples such as
    # ``result_review\.empty_section_content``. Treat that as the same id.
    return _normalize_csv_value(value).replace("\\.", ".")


def _split_csv(
    value: str | None,
    *,
    normalize_scenarios: bool = False,
) -> tuple[str, ...]:
    if not value:
        return ()
    parts: list[str] = []
    for part in value.split(","):
        normalized = (
            _normalize_scenario_id(part)
            if normalize_scenarios
            else _normalize_csv_value(part)
        )
        if normalized:
            parts.append(normalized)
    return tuple(parts)


@dataclass(frozen=True)
class FaultInjectionSettings:
    enabled: bool
    app_env: str
    allowed_envs: frozenset[str]
    scenarios: tuple[str, ...]
    task_ids: frozenset[str]

    @classmethod
    def from_env(cls) -> "FaultInjectionSettings":
        explicit_enabled = os.environ.get("FAULT_INJECTION_ENABLED")
        if explicit_enabled is None:
            enabled = _truthy(os.environ.get("FAULT_ENABLED"))
        else:
            enabled = _truthy(explicit_enabled)

        explicit_scenarios = _split_csv(
            os.environ.get("FAULT_INJECTION_SCENARIOS"),
            normalize_scenarios=True,
        )
        if explicit_scenarios:
            scenarios = explicit_scenarios
        else:
            scenarios = tuple(
                scenario
                for env_name, scenario in LEGACY_SCENARIO_ENV
                if _truthy(os.environ.get(env_name))
            )

        allowed_envs = frozenset(
            env.lower() for env in _split_csv(os.environ.get("FAULT_INJECTION_ALLOWED_ENV"))
        ) or DEFAULT_ALLOWED_ENVS

        task_ids = frozenset(_split_csv(os.environ.get("FAULT_INJECTION_TASK_IDS")))

        return cls(
            enabled=enabled,
            app_env=(os.environ.get("APP_ENV") or "development").strip().lower(),
            allowed_envs=allowed_envs,
            scenarios=scenarios,
            task_ids=task_ids,
        )

    def enabled_for(self, context: dict[str, Any] | None) -> bool:
        if not self.enabled:
            return False
        if self.app_env in PROD_ENVS:
            return False
        if self.app_env not in self.allowed_envs:
            return False
        if self.task_ids:
            task_id = str((context or {}).get("task_id") or "")
            return task_id in self.task_ids
        return True
