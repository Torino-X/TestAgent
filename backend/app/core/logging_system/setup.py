"""Idempotent, fail-safe handler configuration for the TestAgent backend."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LoggingSettings, logging_settings_from_app_settings
from .filters import StructuredLogFilter
from .formatter import JsonFormatter, PrettyConsoleFormatter
from app.core.observability import ObservabilitySettings, configure_tracing


class SafeRotatingFileHandler(RotatingFileHandler):
    """Never let a disk/rotation failure break the application request path."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        try:
            sys.stderr.write("[testagent logging] file handler failed; continuing without file log\n")
        except Exception:
            pass


def setup_logging(config: LoggingSettings | None = None) -> None:
    if config is None:
        from app.core.config import get_settings
        config = logging_settings_from_app_settings(get_settings())
    root = logging.getLogger()
    _remove_owned_handlers(root)
    root.setLevel(getattr(logging, config.level, logging.INFO))
    for logger_name, level_name in (config.domain_levels or {}).items():
        logging.getLogger(logger_name).setLevel(getattr(logging, level_name.upper(), logging.INFO))
    configure_tracing(ObservabilitySettings(enabled=config.otel_enabled, service_name=config.otel_service_name, otlp_endpoint=config.otel_exporter_otlp_endpoint, sample_rate=config.otel_sample_rate))
    formatter = JsonFormatter(environment=config.environment, schema_version=config.schema_version, max_event_bytes=config.max_event_bytes)
    filter_ = StructuredLogFilter(environment=config.environment, redaction_enabled=True if config.is_production else config.redaction_enabled, schema_version=config.schema_version, sampling_enabled=config.sampling_enabled, success_sample_rate=config.success_sample_rate, health_sample_rate=config.health_sample_rate, rate_limit_enabled=config.rate_limit_enabled, rate_limit_window_seconds=config.rate_limit_window_seconds)
    if config.console_enabled:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter if config.is_production else PrettyConsoleFormatter())
        console.addFilter(filter_)
        _mark_owned(console)
        root.addHandler(console)
    if config.file_enabled and not config.is_production:
        try:
            _add_development_files(root, config, formatter, filter_)
        except Exception:
            try:
                sys.stderr.write("[testagent logging] cannot initialise development file logging; continuing on console\n")
            except Exception:
                pass
    elif config.file_enabled and config.is_production:
        try:
            sys.stderr.write("[testagent logging] LOG_FILE_ENABLED ignored in production; JSON stdout remains active\n")
        except Exception:
            pass
    logging.captureWarnings(True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _add_development_files(root: logging.Logger, config: LoggingSettings, formatter: logging.Formatter, filter_: logging.Filter) -> None:
    pid = __import__("os").getpid()
    app_dir = Path(config.log_dir) / "app"
    error_dir = Path(config.log_dir) / "error"
    app_dir.mkdir(parents=True, exist_ok=True)
    error_dir.mkdir(parents=True, exist_ok=True)
    for path, level in ((app_dir / f"testagent-pid{pid}.jsonl", logging.NOTSET), (error_dir / f"testagent-error-pid{pid}.jsonl", logging.ERROR)):
        handler = SafeRotatingFileHandler(path, encoding="utf-8", maxBytes=config.file_max_bytes, backupCount=config.file_backup_count)
        handler.setLevel(level)
        handler.setFormatter(formatter)
        handler.addFilter(filter_)
        _mark_owned(handler)
        root.addHandler(handler)


def _mark_owned(handler: logging.Handler) -> None:
    handler._testagent_owned = True  # type: ignore[attr-defined]


def _remove_owned_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if getattr(handler, "_testagent_owned", False):
            root.removeHandler(handler)
            handler.close()
