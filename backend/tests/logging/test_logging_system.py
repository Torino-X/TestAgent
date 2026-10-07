from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import subprocess
import sys

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.core.logging import (
    JsonFormatter,
    LoggingSettings,
    RequestLoggingMiddleware,
    bind_log_context,
    clear_log_context,
    get_log_context,
    redact_value,
    setup_logging,
)
from app.core.logging_system.filters import StructuredLogFilter


class TestStructuredFormatter:
    def test_filter_redacts_parameterized_dsn_without_breaking_formatting(self):
        """A sensitive ``key=%s`` placeholder must remain format-safe.

        The filter runs before every handler formats the record.  Redacting
        the template itself used to turn ``dsn=%s`` into ``dsn=***`` while
        leaving the fourth argument in place, which caused ``LogRecord``
        formatting to raise ``TypeError`` during application startup.
        """
        filter_ = StructuredLogFilter(environment="development")
        record = logging.LogRecord(
            "testagent.application",
            logging.INFO,
            __file__,
            1,
            "Postgres verify | tables=%d/%d | %s | dsn=%s",
            (4, 4, ["checkpoints"], "postgresql://user:TEST_DSN_SECRET@host/db"),
            None,
        )

        assert filter_.filter(record)
        rendered = record.getMessage()

        assert "TEST_DSN_SECRET" not in rendered
        assert rendered.endswith("dsn=***")

    def test_json_schema_correlation_and_exception_redaction(self):
        formatter = JsonFormatter(environment="development")
        record = logging.LogRecord(
            "testagent.context",
            logging.ERROR,
            __file__,
            12,
            "connection redis://:REDIS_SECRET@host:6379/0 Bearer TOKEN_SECRET",
            (),
            None,
        )
        record.event = "context.compose.failed"
        record.task_id = "task_123"
        try:
            raise RuntimeError("mysql://user:MYSQL_SECRET@host/db")
        except RuntimeError:
            record.exc_info = __import__("sys").exc_info()

        rendered = json.loads(formatter.format(record))

        assert rendered["event"] == "context.compose.failed"
        assert rendered["task_id"] == "task_123"
        assert rendered["service"] == "testagent-backend"
        assert rendered["level"] == "ERROR"
        assert "REDIS_SECRET" not in json.dumps(rendered)
        assert "TOKEN_SECRET" not in json.dumps(rendered)
        assert "MYSQL_SECRET" not in json.dumps(rendered)
        assert rendered["stacktrace"]

    def test_redaction_is_recursive_and_covers_url_query_and_bearer(self):
        value = {
            "authorization": "Bearer TOP_SECRET",
            "nested": {"password": "PASSWORD_SECRET"},
            "url": "https://host/path?api_key=API_SECRET&safe=value",
            "items": ["postgresql://user:PG_SECRET@host/db", "safe"],
        }

        rendered = redact_value(value)
        encoded = json.dumps(rendered)

        for secret in ("TOP_SECRET", "PASSWORD_SECRET", "API_SECRET", "PG_SECRET"):
            assert secret not in encoded
        assert rendered["url"].endswith("safe=value")

    def test_sensitive_key_with_string_value_is_masked_in_jsonl(self):
        # Regression: when a sensitive key carries a string value (not a
        # nested mapping) the formatter must mask the entire value,
        # because regex-based redact_text() cannot infer the parent key.
        formatter = JsonFormatter(environment="development")
        record = logging.LogRecord(
            "testagent.application",
            logging.INFO,
            __file__,
            1,
            "msg",
            (),
            None,
        )
        record.cookie = "session=TOP_COOKIE"
        record.api_key = "TOP_KEY"
        record.authorization = "Bearer TOP_BEARER"

        rendered = json.loads(formatter.format(record))
        encoded = json.dumps(rendered)

        for secret in ("TOP_COOKIE", "TOP_KEY", "TOP_BEARER"):
            assert secret not in encoded
        assert rendered["cookie"] == "***"
        assert rendered["api_key"] == "***"
        assert rendered["authorization"] == "***"


class TestCorrelationContext:
    @pytest.mark.asyncio
    async def test_context_isolated_between_concurrent_tasks(self):
        async def worker(task_id: str):
            bind_log_context(task_id=task_id, project_id=f"project-{task_id}")
            await asyncio.sleep(0)
            result = get_log_context()
            clear_log_context()
            return result

        first, second = await asyncio.gather(worker("A"), worker("B"))

        assert first == {"task_id": "A", "project_id": "project-A"}
        assert second == {"task_id": "B", "project_id": "project-B"}
        assert get_log_context() == {}


class TestHandlersAndMiddleware:
    def test_development_uses_pid_jsonl_and_error_file_but_production_does_not(self, tmp_path: Path):
        root = logging.getLogger()
        original_handlers = list(root.handlers)
        try:
            setup_logging(
                LoggingSettings(
                    environment="development",
                    console_enabled=False,
                    file_enabled=True,
                    log_dir=tmp_path,
                    file_max_bytes=1024,
                    file_backup_count=1,
                )
            )
            logger = logging.getLogger("testagent.application")
            logger.info("app event", extra={"event": "application.started"})
            logger.error("error event", extra={"event": "application.failed"})
            for handler in root.handlers:
                handler.flush()

            app_logs = list((tmp_path / "app").glob("testagent-pid*.jsonl"))
            error_logs = list((tmp_path / "error").glob("testagent-error-pid*.jsonl"))
            assert len(app_logs) == len(error_logs) == 1
            assert json.loads(app_logs[0].read_text(encoding="utf-8").splitlines()[0])["event"] == "application.started"
            assert json.loads(error_logs[0].read_text(encoding="utf-8").splitlines()[0])["level"] == "ERROR"

            setup_logging(
                LoggingSettings(
                    environment="production",
                    console_enabled=False,
                    file_enabled=True,
                    log_dir=tmp_path / "forbidden",
                )
            )
            assert not (tmp_path / "forbidden").exists()
        finally:
            for handler in list(root.handlers):
                if getattr(handler, "_testagent_owned", False):
                    root.removeHandler(handler)
                    handler.close()
            for handler in original_handlers:
                if handler not in root.handlers:
                    root.addHandler(handler)

    def test_unwritable_log_target_does_not_break_application_logging(self, tmp_path: Path):
        target_file = tmp_path / "not-a-directory"
        target_file.write_text("occupied", encoding="utf-8")
        root = logging.getLogger()
        original_handlers = list(root.handlers)
        try:
            setup_logging(
                LoggingSettings(
                    environment="development",
                    console_enabled=False,
                    file_enabled=True,
                    log_dir=target_file,
                )
            )
            logging.getLogger("testagent.application").info(
                "business path remains available", extra={"event": "application.test"}
            )
        finally:
            for handler in list(root.handlers):
                if getattr(handler, "_testagent_owned", False):
                    root.removeHandler(handler)
                    handler.close()
            for handler in original_handlers:
                if handler not in root.handlers:
                    root.addHandler(handler)

    @pytest.mark.asyncio
    async def test_middleware_adds_safe_request_id_and_cleans_context(self):
        app = FastAPI()
        app.add_middleware(RequestLoggingMiddleware)

        @app.get("/check")
        async def check():
            return JSONResponse({"context": get_log_context()})

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/check", headers={"X-Request-ID": "bad\ninput"})

        assert response.status_code == 200
        assert response.headers["X-Request-ID"].startswith("req_")
        assert response.json()["context"]["request_id"] == response.headers["X-Request-ID"]
        assert get_log_context() == {}

    def test_query_cli_filters_combined_fields_and_ignores_invalid_jsonl(self, tmp_path: Path):
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "testagent-pid1.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"timestamp": "2026-01-01T00:00:00Z", "task_id": "task_a", "level": "INFO", "event": "agent.task.started"}),
                    "not-json",
                    json.dumps({"timestamp": "2026-01-01T00:00:01Z", "task_id": "task_a", "level": "ERROR", "event": "agent.task.failed"}),
                    json.dumps({"timestamp": "2026-01-01T00:00:02Z", "task_id": "task_b", "level": "ERROR", "event": "agent.task.failed"}),
                ]
            ),
            encoding="utf-8",
        )
        script = Path(__file__).parents[2] / "scripts" / "log_query.py"
        result = subprocess.run(
            [sys.executable, str(script), "--log-dir", str(tmp_path), "--task-id", "task_a", "--level", "ERROR"],
            check=True,
            capture_output=True,
            text=True,
        )

        assert [json.loads(line) for line in result.stdout.splitlines()] == [
            {"timestamp": "2026-01-01T00:00:01Z", "task_id": "task_a", "level": "ERROR", "event": "agent.task.failed"}
        ]
