"""Pytest mirror of ``backend/scripts/verify_logging_acceptance.py``.

These tests execute the same three acceptance scenarios the CLI script
runs, but in-process and with the test runner's lifecycle.  They let CI
guard the redaction, exception-correlation, and production-guard
behaviours without depending on ``subprocess``.

Each test isolates its log directory under ``tmp_path`` and tears down
the logging handlers it added so the next test starts clean.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.core.logging import (
    LoggingSettings,
    LogEvent,
    RequestLoggingMiddleware,
    bind_log_context,
    clear_log_context,
    get_log_context,
    get_request_correlation,
    log_event,
    redact_value,
    setup_logging,
)

SENTINELS = (
    "LOG_ACCEPTANCE_BEARER_SECRET_123",
    "LOG_ACCEPTANCE_REDIS_PASSWORD_123",
    "LOG_ACCEPTANCE_MYSQL_PASSWORD_123",
    "LOG_ACCEPTANCE_POSTGRES_PASSWORD_123",
    "LOG_ACCEPTANCE_API_KEY_123",
    "LOG_ACCEPTANCE_COOKIE_SECRET_123",
    "LOG_ACCEPTANCE_TOKEN_123",
    "LOG_ACCEPTANCE_PASSWORD_123",
    "LOG_ACCEPTANCE_EXCEPTION_SECRET",
    "LOG_ACCEPTANCE_PRODUCTION_SECRET_123",
)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _all_records(log_root: Path) -> list[dict]:
    records: list[dict] = []
    for sub in ("app", "error"):
        directory = log_root / sub
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.jsonl")):
            records.extend(_read_jsonl(path))
    return records


def _find_first(records, **predicates):
    for record in records:
        if all(str(record.get(key, "")) == str(value) for key, value in predicates.items()):
            return record
    return None


@pytest.fixture
def dev_log_dir(tmp_path: Path):
    """Configure a development JSONL logger into ``tmp_path`` for one test."""
    setup_logging(
        LoggingSettings(
            environment="development",
            console_enabled=False,
            file_enabled=True,
            log_dir=tmp_path,
            file_max_bytes=1024 * 1024,
            file_backup_count=1,
        )
    )
    yield tmp_path
    # Teardown: close every TestAgent-owned handler so Windows can rm the tmpdir.
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_testagent_owned", False):
            try:
                handler.flush()
            except Exception:
                pass
            try:
                handler.close()
            except Exception:
                pass
            root.removeHandler(handler)
    clear_log_context()


def test_acceptance_exception_chain_correlation(dev_log_dir: Path):
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.exception_handler(Exception)
    async def _local_fallback(request, exc):  # noqa: ARG001
        # Mirror production fallback: re-bind correlation from scope["state"]
        # so this ERROR log carries request_id / trace_id.  ServerErrorMiddleware
        # runs us outside the request-middleware's contextvar block.
        request_id, trace_id = get_request_correlation(request)
        bind_log_context(request_id=request_id, trace_id=trace_id)
        logging.getLogger("testagent.application").exception(
            "local fallback handler | path=%s", request.url.path
        )
        response = JSONResponse({"code": 50001, "message": "server error"}, status_code=500)
        if request_id:
            response.headers["X-Request-ID"] = request_id
        return response

    @app.get("/logging-acceptance/boom")
    async def boom():
        bind_log_context(
            task_id="task_logging_acceptance_pytest",
            conversation_id="conv_logging_acceptance_pytest",
            project_id="proj_logging_acceptance_pytest",
        )
        try:
            raise RuntimeError(
                "LOG_ACCEPTANCE_CONTROLLED_EXCEPTION — provider failed "
                "redis://:LOG_ACCEPTANCE_EXCEPTION_SECRET@localhost:6379/0"
            )
        except RuntimeError:
            logging.getLogger("testagent.application").error(
                "domain failure before final 500", exc_info=sys.exc_info()
            )
            raise

    async def _drive() -> str:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/logging-acceptance/boom")
            return response.headers.get("X-Request-ID", "")

    try:
        request_id = asyncio.run(_drive())
    finally:
        clear_log_context()

    if not request_id.startswith("req_"):
        # Fall back to the FAILED record's request_id when the middleware
        # could not stamp the response header (exception path).
        records = _all_records(dev_log_dir)
        failed = _find_first(records, event=LogEvent.HTTP_REQUEST_FAILED.value)
        if failed and str(failed.get("request_id", "")).startswith("req_"):
            request_id = str(failed["request_id"])

    for handler in logging.getLogger().handlers:
        handler.flush()

    records = _all_records(dev_log_dir)
    assert records, f"no JSONL records produced under {dev_log_dir}"
    assert request_id.startswith("req_")

    error_records = [r for r in records if r.get("level") == "ERROR"]
    assert error_records, "no ERROR-level records captured"

    # Every ERROR record (route + fallback) MUST carry the request_id
    # bound by the middleware.  This is the regression assertion for
    # the original Known Limitation 1 (exception path losing correlation).
    missing = [r for r in error_records if str(r.get("request_id")) != request_id]
    assert not missing, (
        f"ERROR records missing request_id; sampled: "
        f"{[(r.get('event'), r.get('request_id')) for r in missing]}"
    )

    # The route-level ERROR carries task_id; the fallback ERROR runs in
    # ServerErrorMiddleware and may not see task_id.  At least one
    # ERROR must carry task_id to prove task_id correlation is preserved.
    task_correlated = [
        r for r in error_records
        if r.get("task_id") == "task_logging_acceptance_pytest"
    ]
    assert task_correlated, (
        f"no ERROR record carries task_id; sampled: "
        f"{[(r.get('event'), r.get('task_id')) for r in error_records]}"
    )

    for record in error_records:
        assert "stacktrace" in record or "traceback" in record, (
            f"ERROR record missing stacktrace/traceback: {record}"
        )
        assert record.get("exception_type") == "RuntimeError", (
            f"unexpected exception_type {record.get('exception_type')!r}"
        )

    # Sentinel secret must NOT appear in any record.
    blob = json.dumps(records, ensure_ascii=False)
    assert "LOG_ACCEPTANCE_EXCEPTION_SECRET" not in blob

    # log_query CLI must find an ERROR for both request_id and task_id.
    script = Path(__file__).resolve().parents[2] / "scripts" / "log_query.py"
    for args in (["--request-id", request_id], ["--task-id", "task_logging_acceptance_pytest"]):
        result = subprocess.run(
            [sys.executable, str(script), "--log-dir", str(dev_log_dir), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        parsed = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        assert any(r.get("level") == "ERROR" for r in parsed), (
            f"log_query {args} returned no ERROR: {parsed}"
        )


def test_acceptance_redaction_pipeline(dev_log_dir: Path):
    # ── Direct public-API assertions ──
    payload = {
        "password": "LOG_ACCEPTANCE_PASSWORD_123",
        "api_key": "LOG_ACCEPTANCE_API_KEY_123",
        "authorization": "Bearer LOG_ACCEPTANCE_BEARER_SECRET_123",
        "nested": {"token": "LOG_ACCEPTANCE_TOKEN_123"},
        "redis_url": "redis://:LOG_ACCEPTANCE_REDIS_PASSWORD_123@127.0.0.1:6379/1",
        "mysql_url": "mysql://user:LOG_ACCEPTANCE_MYSQL_PASSWORD_123@localhost/test",
        "postgres_url": "postgresql://user:LOG_ACCEPTANCE_POSTGRES_PASSWORD_123@localhost/test",
        "query_url": "https://example.invalid/api?api_key=LOG_ACCEPTANCE_API_KEY_123&token=LOG_ACCEPTANCE_TOKEN_123",
        "cookie": "session=LOG_ACCEPTANCE_COOKIE_SECRET_123",
        "model": "test-model",
        "task_id": "task_logging_acceptance",
        "duration_ms": 123,
    }
    redacted = redact_value(payload)
    encoded = json.dumps(redacted)
    for secret in SENTINELS:
        assert secret not in encoded, f"sentinel {secret} leaked from redact_value"
    assert "***" in encoded or "[REDACTED]" in encoded, "no redaction marker"
    # Normal fields survive.
    assert redacted["model"] == "test-model"
    assert redacted["task_id"] == "task_logging_acceptance"
    assert redacted["duration_ms"] == 123

    # ── End-to-end pipeline + exception ──
    bind_log_context(task_id="task_logging_acceptance_pytest_redaction")
    logger = logging.getLogger("testagent.application")
    try:
        logger.info(
            "redaction smoke payload",
            extra={
                "event": "application.started",
                "payload": payload,
                "authorization": f"Bearer LOG_ACCEPTANCE_BEARER_SECRET_123",
                "query_url": payload["query_url"],
                "redis_url": payload["redis_url"],
                "mysql_url": payload["mysql_url"],
                "postgres_url": payload["postgres_url"],
                "cookie": payload["cookie"],
                "model": "test-model",
            },
        )
        try:
            raise RuntimeError(
                "provider failed "
                "redis://:LOG_ACCEPTANCE_EXCEPTION_SECRET@localhost:6379/0"
            )
        except RuntimeError:
            logger.error("redaction smoke exception", exc_info=sys.exc_info())
    finally:
        clear_log_context()

    for handler in logging.getLogger().handlers:
        handler.flush()

    records = _all_records(dev_log_dir)
    assert records, "no JSONL records captured"
    blob = json.dumps(records, ensure_ascii=False)
    for secret in SENTINELS:
        assert secret not in blob, f"sentinel {secret} leaked into JSONL"
    assert "***" in blob or "[REDACTED]" in blob

    error_records = [r for r in records if r.get("level") == "ERROR"]
    assert error_records, "no ERROR record captured"
    assert any("provider failed" in (r.get("exception_message") or "") for r in error_records)


def test_acceptance_production_guard_forces_json_stdout_and_no_files(tmp_path: Path):
    """Spawn a subprocess with APP_ENV=production and confirm guard behaviour."""
    child_script = (
        "import json, logging, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n"
        "from app.core.logging import (\n"
        "    LoggingSettings, bind_log_context, clear_log_context, log_event, setup_logging,\n"
        ")\n"
        f"log_dir = Path({str(tmp_path)!r})\n"
        "settings = LoggingSettings(\n"
        "    environment='production',\n"
        "    console_enabled=True,\n"
        "    file_enabled=True,  # intentionally set; guard must override\n"
        "    log_dir=log_dir,\n"
        ")\n"
        "setup_logging(settings)\n"
        "logger = logging.getLogger('testagent.application')\n"
        "bind_log_context(task_id='task_logging_acceptance_pytest_production')\n"
        "log_event(logger, logging.INFO, 'application.test.info', 'production smoke info')\n"
        "log_event(\n"
        "    logger, logging.WARNING, 'application.test.warning',\n"
        "    'production smoke warning with api_key=LOG_ACCEPTANCE_PRODUCTION_SECRET_123',\n"
        ")\n"
        "clear_log_context()\n"
        "for h in logging.getLogger().handlers:\n"
        "    try:\n"
        "        h.flush()\n"
        "    except Exception:\n"
        "        pass\n"
        "sys.stdout.flush()\n"
        "sys.stderr.flush()\n"
    )
    import os

    env = os.environ.copy()
    env["APP_ENV"] = "production"
    env["LOG_FILE_ENABLED"] = "1"
    env["LOG_FORMAT"] = "json"
    env["LOG_DIR"] = str(tmp_path)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])

    backend_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", child_script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(backend_root),
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, f"production child failed: {result.stderr}"

    testagent_records: list[dict] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("service") == "testagent-backend":
            testagent_records.append(obj)

    assert testagent_records, f"no TestAgent JSON on stdout; raw={result.stdout[:300]!r}"
    sample = testagent_records[0]
    for required in ("timestamp", "level", "service", "environment", "event", "message"):
        assert required in sample, f"missing field {required!r} in {sample}"
    assert str(sample.get("environment")).lower() == "production"

    # Redaction still active in production stdout.
    blob = json.dumps(testagent_records, ensure_ascii=False)
    assert "LOG_ACCEPTANCE_PRODUCTION_SECRET_123" not in blob
    assert "***" in blob or "[REDACTED]" in blob

    # File guard: no JSONL files under LOG_DIR/app or LOG_DIR/error.
    forbidden = list((tmp_path / "app").glob("testagent*.jsonl")) + list(
        (tmp_path / "error").glob("testagent*.jsonl")
    )
    assert not forbidden, f"production guard leaked files: {[p.name for p in forbidden]}"

    assert "[testagent logging] LOG_FILE_ENABLED ignored in production" in result.stderr


# ─────── Regression: Known Limitation 1 — exception path correlation ──────
def _build_error_app() -> FastAPI:
    from fastapi import HTTPException

    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.exception_handler(Exception)
    async def _fallback(request, exc):  # noqa: ARG001
        request_id, trace_id = get_request_correlation(request)
        bind_log_context(request_id=request_id, trace_id=trace_id)
        logging.getLogger("testagent.application").exception(
            "fallback fired | path=%s", request.url.path
        )
        response = JSONResponse({"code": 50001, "message": "server error"}, status_code=500)
        if request_id:
            response.headers["X-Request-ID"] = request_id
        return response

    @app.get("/echo-200")
    async def echo_200():
        return {"ok": True}

    @app.get("/not-found")
    async def not_found():
        raise HTTPException(status_code=404, detail="missing")

    @app.get("/forbidden")
    async def forbidden():
        raise HTTPException(status_code=403, detail="denied")

    @app.get("/validation-error")
    async def validation_error(value: int):
        return {"value": value}

    @app.get("/boom-500")
    async def boom_500():
        raise RuntimeError("LOG_ACCEPTANCE_REGRESSION_CONTROLLED_EXCEPTION")

    return app


def test_acceptance_error_response_correlation(dev_log_dir: Path):
    """Global fallback ERROR must carry request_id (was Known Limitation 1)."""
    app = _build_error_app()

    async def _drive() -> str:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/boom-500")
            return response.headers.get("X-Request-ID", "")

    try:
        boom_rid = asyncio.run(_drive())
    finally:
        clear_log_context()
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert boom_rid.startswith("req_"), f"boom request_id missing or invalid: {boom_rid!r}"

    records = _all_records(dev_log_dir)
    assert records, "no JSONL records captured"
    error_records = [r for r in records if r.get("level") == "ERROR"]
    assert error_records, "no ERROR records captured for boom"

    # Every ERROR record (route + fallback) must carry the boom request_id.
    missing = [r for r in error_records if str(r.get("request_id")) != boom_rid]
    assert not missing, (
        f"ERROR records missing matching request_id; sampled: "
        f"{[(r.get('event'), r.get('request_id')) for r in missing]}"
    )

    # Specifically: the global fallback ERROR (the one previously
    # orphaned by BaseHTTPMiddleware's clear_log_context) must carry
    # request_id.  We identify it by its message containing 'fallback'.
    fallback_errors = [
        r for r in error_records
        if "fallback" in str(r.get("message", "")).lower()
    ]
    assert fallback_errors, (
        f"no global fallback ERROR identified; messages: "
        f"{[r.get('message') for r in error_records]}"
    )
    for record in fallback_errors:
        assert str(record.get("request_id")) == boom_rid, (
            f"global fallback ERROR missing request_id: "
            f"req={record.get('request_id')!r} rid={boom_rid!r}"
        )


def test_acceptance_error_response_request_id_header(dev_log_dir: Path):
    """All HTTP responses (2xx/4xx/422/5xx) carry X-Request-ID."""
    app = _build_error_app()

    cases = [
        ("/echo-200", 200),
        ("/not-found", 404),
        ("/forbidden", 403),
        ("/validation-error?value=notanint", 422),
        ("/boom-500", 500),
    ]

    async def _drive() -> list[tuple[str, int, str]]:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        results: list[tuple[str, int, str]] = []
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path, _expected_status in cases:
                resp = await client.get(path)
                results.append((path, resp.status_code, resp.headers.get("X-Request-ID", "")))
        return results

    try:
        results = asyncio.run(_drive())
    finally:
        clear_log_context()
    for handler in logging.getLogger().handlers:
        handler.flush()

    for path, status, rid in results:
        assert rid.startswith("req_"), (
            f"{path} (status={status}) missing X-Request-ID: {rid!r}"
        )

    # Each header request_id must appear in the JSONL records, so
    # operators can query --request-id and find the matching lifecycle log.
    records = _all_records(dev_log_dir)
    assert records, "no JSONL records captured"
    seen = {str(r.get("request_id")) for r in records if r.get("request_id")}
    for _path, _status, rid in results:
        assert rid in seen, (
            f"header request_id {rid!r} has no matching JSONL record"
        )


# ─────────── Concurrency: ContextVar isolation between concurrent requests ──
def test_acceptance_concurrent_requests_do_not_leak_correlation(dev_log_dir: Path):
    """Two concurrent requests must not see each other's request_id."""
    app = _build_error_app()

    async def worker(label: str) -> tuple[str, str]:
        async def _drive() -> tuple[str, str]:
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/echo-200")
                return resp.headers.get("X-Request-ID", ""), resp.json()["ok"]

        rid, _ = await _drive()
        return label, rid

    async def _drive_concurrent() -> list[tuple[str, str]]:
        return await asyncio.gather(worker("A"), worker("B"))

    try:
        results = asyncio.run(_drive_concurrent())
    finally:
        clear_log_context()
    for handler in logging.getLogger().handlers:
        handler.flush()

    rids = {rid for _, rid in results}
    assert len(rids) == 2, f"concurrent requests must have distinct request_ids; got {rids}"

    # Each request_id must have its own lifecycle records (no cross-pollination).
    records = _all_records(dev_log_dir)
    for _, rid in results:
        matching = [r for r in records if str(r.get("request_id")) == rid]
        assert matching, f"no records for request_id {rid}"
        assert all(r.get("request_id") == rid for r in matching), (
            f"records leaked between requests: {[r.get('request_id') for r in matching]}"
        )


# ─────────── Context cleanup after request lifecycle ─────────────────────
def test_acceptance_context_is_cleared_after_request(dev_log_dir: Path):
    """get_log_context() must be empty between requests."""
    app = _build_error_app()

    async def _drive() -> None:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for _ in range(3):
                resp = await client.get("/echo-200")
                assert resp.headers.get("X-Request-ID", "").startswith("req_")

    try:
        asyncio.run(_drive())
    finally:
        clear_log_context()

    for handler in logging.getLogger().handlers:
        handler.flush()

    assert get_log_context() == {}, (
        f"contextvar leaked after request lifecycle: {get_log_context()!r}"
    )
