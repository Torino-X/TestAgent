"""Logging-only configuration projected from the application's settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    environment: str = "development"
    level: str = "INFO"
    console_enabled: bool = True
    file_enabled: bool = True
    log_dir: Path = Path("./logs")
    file_max_bytes: int = 50 * 1024 * 1024
    file_backup_count: int = 10
    redaction_enabled: bool = True
    domain_levels: Mapping[str, str] | None = None
    schema_version: str = "2.0"
    sampling_enabled: bool = True
    success_sample_rate: float = 1.0
    health_sample_rate: float = 0.01
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    max_event_bytes: int = 32 * 1024
    incident_bundle_enabled: bool = True
    otel_enabled: bool = False
    otel_service_name: str = "testagent-backend"
    otel_exporter_otlp_endpoint: str = ""
    otel_sample_rate: float = 1.0

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


def logging_settings_from_app_settings(settings: object) -> LoggingSettings:
    """Read declared application settings without reaching into environment directly."""
    return LoggingSettings(
        environment=str(getattr(settings, "app_env", "development")),
        level=str(getattr(settings, "log_level", "INFO")).upper(),
        console_enabled=bool(getattr(settings, "log_console_enabled", True)),
        file_enabled=bool(getattr(settings, "log_file_enabled", True)),
        log_dir=Path(str(getattr(settings, "log_dir", "./logs"))),
        file_max_bytes=int(getattr(settings, "log_file_max_bytes", 50 * 1024 * 1024)),
        file_backup_count=int(getattr(settings, "log_file_backup_count", 10)),
        redaction_enabled=bool(getattr(settings, "log_redaction_enabled", True)),
        schema_version=str(getattr(settings, "log_schema_version", "2.0")),
        sampling_enabled=bool(getattr(settings, "log_sampling_enabled", True)),
        success_sample_rate=float(getattr(settings, "log_success_sample_rate", 1.0)),
        health_sample_rate=float(getattr(settings, "log_health_sample_rate", 0.01)),
        rate_limit_enabled=bool(getattr(settings, "log_rate_limit_enabled", True)),
        rate_limit_window_seconds=int(getattr(settings, "log_rate_limit_window_seconds", 60)),
        max_event_bytes=int(getattr(settings, "log_max_event_bytes", 32 * 1024)),
        incident_bundle_enabled=bool(getattr(settings, "log_incident_bundle_enabled", True)),
        otel_enabled=bool(getattr(settings, "otel_enabled", False)),
        otel_service_name=str(getattr(settings, "otel_service_name", "testagent-backend")),
        otel_exporter_otlp_endpoint=str(getattr(settings, "otel_exporter_otlp_endpoint", "")),
        otel_sample_rate=float(getattr(settings, "otel_sample_rate", 1.0)),
        domain_levels={
            "testagent.http": str(getattr(settings, "log_level_http", "INFO")),
            "testagent.application": str(getattr(settings, "log_level_application", "INFO")),
            "testagent.agent": str(getattr(settings, "log_level_agent", "INFO")),
            "testagent.context": str(getattr(settings, "log_level_context", "INFO")),
            "testagent.integration": str(getattr(settings, "log_level_integration", "INFO")),
            "testagent.security": str(getattr(settings, "log_level_security", "INFO")),
        },
    )
