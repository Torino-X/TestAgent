"""Safe, repeatable Logging Acceptance harness for the TestAgent backend.

Three acceptance scenarios verify that the real TestAgent logging pipeline
(``app.core.logging``) emits the records production would rely on:

* ``exception``        — controlled exception produces an ERROR JSONL record
                          with stacktrace + request_id + task_id and is
                          discoverable through ``log_query`` CLI.
* ``redaction``        — Bearer / URL credential / query-string / nested
                          field / exception message / stack-trace secrets
                          are redacted and normal fields survive.
* ``production-guard`` — APP_ENV=production child process emits JSON to
                          stdout, never creates JSONL files, keeps redaction.

All output stays inside the user's temporary directory; no real secrets,
no production data, no real LLM / Redis / Postgres / Qdrant / ES / OSS
contacts.  Every JSONL/SQL/file mutation lands in :class:`tempfile.TemporaryDirectory`
or in ``backend/tmp/logging_acceptance/`` (gitignored).

Exit code = 0 only when every enabled scenario reports ``PASS``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import traceback
from pathlib import Path
from typing import Any, Iterable

# Make ``app`` importable when this script is invoked as
# ``python scripts/verify_logging_acceptance.py …`` from anywhere.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

# ─── Constants used as sentinels for the redaction acceptance scenario ───
# Only ever referenced from inside controlled payloads — never derived from
# real env / settings / DB rows.
SENTINEL_BEARER = "LOG_ACCEPTANCE_BEARER_SECRET_123"
SENTINEL_REDIS_PASSWORD = "LOG_ACCEPTANCE_REDIS_PASSWORD_123"
SENTINEL_MYSQL_PASSWORD = "LOG_ACCEPTANCE_MYSQL_PASSWORD_123"
SENTINEL_POSTGRES_PASSWORD = "LOG_ACCEPTANCE_POSTGRES_PASSWORD_123"
SENTINEL_API_KEY = "LOG_ACCEPTANCE_API_KEY_123"
SENTINEL_COOKIE_SECRET = "LOG_ACCEPTANCE_COOKIE_SECRET_123"
SENTINEL_OSS_SECRET = "LOG_ACCEPTANCE_OSS_SECRET_123"
SENTINEL_TOKEN = "LOG_ACCEPTANCE_TOKEN_123"
SENTINEL_PASSWORD = "LOG_ACCEPTANCE_PASSWORD_123"
SENTINEL_EXCEPTION_SECRET = "LOG_ACCEPTANCE_EXCEPTION_SECRET"
SENTINEL_PRODUCTION_SECRET = "LOG_ACCEPTANCE_PRODUCTION_SECRET_123"

# Correlation identifiers that obviously read as test-only.
REQUEST_ID_SENTINEL = "req_logging_acceptance_001"
TRACE_ID_SENTINEL = "trace_logging_acceptance_001"
TASK_ID_SENTINEL = "task_logging_acceptance_001"
CONVERSATION_ID_SENTINEL = "conv_logging_acceptance_001"
PROJECT_ID_SENTINEL = "proj_logging_acceptance_001"

ALL_SENTINEL_SECRETS: tuple[str, ...] = (
    SENTINEL_BEARER,
    SENTINEL_REDIS_PASSWORD,
    SENTINEL_MYSQL_PASSWORD,
    SENTINEL_POSTGRES_PASSWORD,
    SENTINEL_API_KEY,
    SENTINEL_COOKIE_SECRET,
    SENTINEL_OSS_SECRET,
    SENTINEL_TOKEN,
    SENTINEL_PASSWORD,
    SENTINEL_EXCEPTION_SECRET,
    SENTINEL_PRODUCTION_SECRET,
)


# ─────────────────────────── helpers ────────────────────────────────────────
def _scratch_dir() -> Path:
    """Return a stable scratch dir for failure diagnostics (gitignored)."""
    scratch = Path(__file__).resolve().parent.parent / "tmp" / "logging_acceptance"
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse every line of *path* as JSON, silently skipping malformed lines."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _all_records(log_root: Path) -> list[dict[str, Any]]:
    """Return every JSONL record produced under ``log_root/app`` and ``.../error``."""
    records: list[dict[str, Any]] = []
    for sub in ("app", "error"):
        directory = log_root / sub
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.jsonl")):
            records.extend(_read_jsonl(path))
    return records


def _find_first(records: Iterable[dict[str, Any]], **predicates: Any) -> dict[str, Any] | None:
    """Return the first record where every predicate matches the record value."""
    for record in records:
        if all(str(record.get(key, "")) == str(value) for key, value in predicates.items()):
            return record
    return None


def _assert_no_sentinel(text: str, *, context: str) -> None:
    """Raise if any sentinel secret is leaked into *text*."""
    for secret in ALL_SENTINEL_SECRETS:
        if secret in text:
            raise AssertionError(
                f"Sentinel secret leaked in {context}: {secret[:32]}<truncated>"
            )


def _print_fail(scenario: str, reason: str, evidence: Any, log_files: list[Path]) -> None:
    print(f"[FAIL] {scenario}")
    print("原因:")
    for line in str(reason).splitlines():
        print(f"  {line}")
    print("证据:")
    if isinstance(evidence, str):
        for line in evidence.splitlines():
            print(f"  {line}")
    else:
        print(f"  {evidence!r}")
    print("日志文件:")
    for path in log_files:
        print(f"  {path}")


def _print_pass(scenario: str) -> None:
    print(f"[PASS] {scenario}")


def _run_query_cli(log_dir: Path, *args: str) -> list[dict[str, Any]]:
    """Invoke ``log_query.py`` against *log_dir* and return its parsed records."""
    script = Path(__file__).resolve().parent / "log_query.py"
    cmd = [sys.executable, str(script), "--log-dir", str(log_dir), *args]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(
            f"log_query.py exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


# ─────────────────────── Scenario A: Exception ─────────────────────────────
def run_exception_acceptance(*, artifact_root: Path) -> tuple[bool, str]:
    """Drive a controlled exception through the real pipeline + middleware."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    import httpx

    from app.core.logging import (
        RequestLoggingMiddleware,
        bind_log_context,
        clear_log_context,
        get_log_context,
        log_event,
        setup_logging,
        LoggingSettings,
        LogEvent,
    )

    log_dir = artifact_root
    log_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(
        LoggingSettings(
            environment="development",
            console_enabled=False,
            file_enabled=True,
            log_dir=log_dir,
            file_max_bytes=1024 * 1024,
            file_backup_count=1,
        )
    )

    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.exception_handler(Exception)
    async def _local_fallback(request, exc):  # type: ignore[no-untyped-def]
        # Mirror production's fallback: re-bind correlation from the
        # middleware-stamped ``scope['state']`` so this ERROR log carries
        # ``request_id`` / ``trace_id``.  ``ServerErrorMiddleware`` runs
        # this handler outside the request-middleware's contextvar block.
        from app.core.logging import get_request_correlation, bind_log_context

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
        # Bind fake correlation IDs *after* the middleware has populated
        # request_id / trace_id — we only append task/conversation/project
        # so the middleware-injected fields are never clobbered.
        bind_log_context(
            task_id=TASK_ID_SENTINEL,
            conversation_id=CONVERSATION_ID_SENTINEL,
            project_id=PROJECT_ID_SENTINEL,
        )
        # Log a domain-level ERROR *with* exc_info so the formatter
        # captures a real traceback, mirroring production use.
        try:
            raise RuntimeError(
                "LOG_ACCEPTANCE_CONTROLLED_EXCEPTION — provider failed "
                f"redis://:{SENTINEL_EXCEPTION_SECRET}@localhost:6379/0"
            )
        except RuntimeError:
            logging.getLogger("testagent.application").error(
                "domain failure before final 500",
                exc_info=sys.exc_info(),
            )
            raise

    @app.get("/logging-acceptance/context")
    async def context():
        return JSONResponse({"context": get_log_context()})

    captured: dict[str, str] = {}

    async def _drive() -> None:
        # ``raise_app_exceptions=False`` lets the FastAPI exception
        # handler convert the raised RuntimeError into a 500 response
        # without bubbling back through the test client.
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            boom_resp = await client.get("/logging-acceptance/boom")
            captured["status"] = str(boom_resp.status_code)
            captured["request_id"] = boom_resp.headers.get("X-Request-ID", "")
            ctx_resp = await client.get("/logging-acceptance/context")
            captured["ctx_request_id"] = str(ctx_resp.json().get("context", {}).get("request_id", ""))

    try:
        asyncio_run(_drive())
    finally:
        clear_log_context()

    # Flush file handlers so the JSONL is fully on disk.
    for handler in logging.getLogger().handlers:
        handler.flush()

    records = _all_records(log_dir)
    if not records:
        return False, f"no JSONL records produced under {log_dir}"

    # Determine the request_id: prefer the response header (set on 2xx)
    # but fall back to the JSONL ``http.request.failed`` record so the
    # test is meaningful even when the response is a 500 (middleware
    # does not stamp the header on error responses in the current
    # implementation).
    request_id = captured.get("request_id", "")
    if not request_id.startswith("req_"):
        failed = _find_first(records, event=LogEvent.HTTP_REQUEST_FAILED.value)
        if failed is not None and failed.get("request_id", "").startswith("req_"):
            request_id = str(failed["request_id"])
    if not request_id.startswith("req_"):
        return False, (
            "could not determine request_id from response header or "
            "HTTP_REQUEST_FAILED record; "
            f"header={captured.get('request_id')!r}, "
            f"events={[r.get('event') for r in records]}"
        )

    # ── Find the middleware-completed or -failed record for the boom request ──
    lifecycle_record = _find_first(
        records,
        request_id=request_id,
        event=LogEvent.HTTP_REQUEST_FAILED.value,
    ) or _find_first(
        records,
        request_id=request_id,
        event=LogEvent.HTTP_REQUEST_COMPLETED.value,
    )
    if lifecycle_record is None:
        return False, (
            f"could not find HTTP_REQUEST_COMPLETED/FAILED for request_id={request_id}; "
            f"saw events: {[r.get('event') for r in records][:8]}"
        )

    # ── Find the ERROR records (domain + unhandled middleware) ──
    error_records = [r for r in records if r.get("level") == "ERROR"]
    if not error_records:
        return False, f"no ERROR-level records found for request_id={request_id}"

    # Every ERROR record (route-level + global-fallback) MUST carry
    # request_id — that was the original Known Limitation that pure
    # ASGI middleware + ``scope['state']`` rebind in the fallback
    # resolves.
    missing_request_id = [
        r for r in error_records
        if str(r.get("request_id")) != request_id
    ]
    if missing_request_id:
        return False, (
            "ERROR records missing matching request_id: "
            f"{[(r.get('event'), r.get('request_id'), r.get('exception_type')) for r in missing_request_id]}"
        )

    # The route-handler ERROR carries the task_id bound inside the
    # route.  The fallback ERROR (which runs in ``ServerErrorMiddleware``)
    # may not see task_id because that binding happened in the inner
    # middleware scope — we don't require fallback ERRORs to carry it,
    # but at least one ERROR MUST carry task_id so we can prove the
    # task_id correlation survives into a real ERROR record.
    task_correlated = [
        r for r in error_records
        if r.get("task_id") == TASK_ID_SENTINEL
    ]
    if not task_correlated:
        return False, (
            f"no ERROR record carries task_id={TASK_ID_SENTINEL}; "
            f"sampled: {[(r.get('event'), r.get('task_id')) for r in error_records]}"
        )

    # Each ERROR must include an exception_type AND a stacktrace.  We
    # check only the records that actually captured an exception (the
    # HTTP_REQUEST_FAILED warning is also ERROR-adjacent but is at
    # WARNING level, so it is not in error_records).
    for record in error_records:
        if "stacktrace" not in record and "traceback" not in record:
            return False, f"ERROR record missing stacktrace/traceback: {record}"
        if "exception_type" not in record:
            return False, f"ERROR record missing exception_type: {record}"
        if str(record.get("exception_type")) != "RuntimeError":
            return False, f"unexpected exception_type {record.get('exception_type')!r}"

    # ── Duplicate-traceback watch ──
    stack_signatures: list[str] = list(
        {r.get("stacktrace") or r.get("traceback") or "" for r in error_records}
    )
    duplicate_warning = (
        len(error_records) > len(stack_signatures)
        or len(error_records) > 2  # route handler + fallback
    )

    # ── log_query CLI smoke ──
    by_request = _run_query_cli(log_dir, "--request-id", request_id)
    if not any(r.get("level") == "ERROR" for r in by_request):
        return False, f"log_query --request-id returned no ERROR: {by_request}"
    by_task = _run_query_cli(log_dir, "--task-id", TASK_ID_SENTINEL)
    if not any(r.get("level") == "ERROR" for r in by_task):
        return False, f"log_query --task-id returned no ERROR: {by_task}"

    # ── Redaction sanity: exception message + stacktrace must hide sentinel ──
    for record in error_records:
        text = json.dumps(record, ensure_ascii=False)
        if SENTINEL_EXCEPTION_SECRET in text:
            return False, "sentinel secret leaked into ERROR record"

    # ── Header == log: the response header value must equal the request_id
    # used in every ERROR log record.  Pure ASGI middleware + the
    # fallback handler now stamp X-Request-ID on every response.
    header_request_id = captured.get("request_id", "")
    if header_request_id != request_id:
        return False, (
            f"response X-Request-ID {header_request_id!r} != "
            f"log request_id {request_id!r}"
        )

    print(f"  request_id={request_id}")
    print(f"  ERROR records: {len(error_records)} (unique stacktraces: {len(stack_signatures)})")
    if duplicate_warning:
        print("  WARNING: duplicate exception logging (multiple ERROR records share correlation)")

    _print_pass("exception-chain")
    return True, ""


def asyncio_run(coro: Any) -> Any:
    return asyncio.run(coro)


# ─────────────────────── Scenario B: Redaction ──────────────────────────────
def run_redaction_acceptance(*, artifact_root: Path) -> tuple[bool, str]:
    from app.core.logging import (
        LoggingSettings,
        bind_log_context,
        clear_log_context,
        log_event,
        redact_text,
        redact_value,
        setup_logging,
    )

    log_dir = artifact_root
    log_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(
        LoggingSettings(
            environment="development",
            console_enabled=False,
            file_enabled=True,
            log_dir=log_dir,
            file_max_bytes=1024 * 1024,
            file_backup_count=1,
        )
    )

    # ── In-process API assertions against the public redactors ──
    # Note: the current redactor only handles URL credentials for
    # redis/mysql/postgres.  OSS-style URLs and ``oss_secret`` keys are
    # intentionally absent from this payload — see the acceptance
    # report "Known Limitations" for the documented scope.
    nested_payload = {
        "password": SENTINEL_PASSWORD,
        "api_key": SENTINEL_API_KEY,
        "authorization": f"Bearer {SENTINEL_BEARER}",
        "nested": {"token": SENTINEL_TOKEN},
        "redis_url": f"redis://:{SENTINEL_REDIS_PASSWORD}@127.0.0.1:6379/1",
        "mysql_url": f"mysql://user:{SENTINEL_MYSQL_PASSWORD}@localhost/test",
        "postgres_url": f"postgresql://user:{SENTINEL_POSTGRES_PASSWORD}@localhost/test",
        "query_url": (
            "https://example.invalid/api?api_key=" + SENTINEL_API_KEY
            + "&token=" + SENTINEL_TOKEN
        ),
        "cookie": f"session={SENTINEL_COOKIE_SECRET}",
        "model": "test-model",
        "task_id": "task_logging_acceptance",
        "duration_ms": 123,
    }
    redacted = redact_value(nested_payload)
    redacted_json = json.dumps(redacted, ensure_ascii=False)
    try:
        _assert_no_sentinel(redacted_json, context="redact_value(nested_payload)")
    except AssertionError as exc:
        return False, str(exc)

    # Confirm at least one redaction marker appears.
    if "***" not in redacted_json and "[REDACTED]" not in redacted_json:
        return False, (
            "no redaction marker (*** or [REDACTED]) appeared in redacted output; "
            f"got: {redacted_json[:200]}"
        )

    # Confirm non-sensitive fields survive.
    if redacted.get("model") != "test-model":
        return False, f"normal field 'model' was over-redacted: {redacted.get('model')!r}"
    if redacted.get("task_id") != "task_logging_acceptance":
        return False, f"normal field 'task_id' was over-redacted: {redacted.get('task_id')!r}"
    if redacted.get("duration_ms") != 123:
        return False, f"normal field 'duration_ms' was over-redacted: {redacted.get('duration_ms')!r}"

    # ── Drive a structured record through the real pipeline + exception ──
    bind_log_context(task_id="task_logging_acceptance_redaction")
    logger = logging.getLogger("testagent.application")
    try:
        logger.info(
            "redaction smoke payload",
            extra={
                "event": "application.started",
                "payload": nested_payload,
                "authorization": f"Bearer {SENTINEL_BEARER}",
                "query_url": nested_payload["query_url"],
                "redis_url": nested_payload["redis_url"],
                "mysql_url": nested_payload["mysql_url"],
                "postgres_url": nested_payload["postgres_url"],
                "cookie": nested_payload["cookie"],
                "model": "test-model",
            },
        )
        try:
            raise RuntimeError(
                "provider failed "
                f"redis://:{SENTINEL_EXCEPTION_SECRET}@localhost:6379/0"
            )
        except RuntimeError:
            logger.error(
                "redaction smoke exception",
                exc_info=sys.exc_info(),
            )
    finally:
        clear_log_context()

    for handler in logging.getLogger().handlers:
        handler.flush()

    # ── Scan every emitted record for sentinels ──
    records = _all_records(log_dir)
    if not records:
        return False, f"no JSONL records produced under {log_dir}"
    captured_blob = json.dumps(records, ensure_ascii=False)
    try:
        _assert_no_sentinel(captured_blob, context="JSONL records (redaction scenario)")
    except AssertionError as exc:
        return False, str(exc)

    # Confirm the exception message was captured (so the test is actually
    # exercising the traceback / exception-message path).
    error_records = [r for r in records if r.get("level") == "ERROR"]
    if not error_records:
        return False, "no ERROR record captured during redaction scenario"
    if not any("provider failed" in (r.get("exception_message") or "") for r in error_records):
        return False, (
            "exception_message not captured or sanitised to remove marker; "
            f"sample: {error_records[0].get('exception_message')!r}"
        )
    # The exception message text contains the sentinel URL — confirm
    # the redactor stripped it from both message and stacktrace.
    for record in error_records:
        for field in ("exception_message", "message", "stacktrace", "traceback"):
            value = record.get(field) or ""
            if value and SENTINEL_EXCEPTION_SECRET in str(value):
                return False, (
                    f"sentinel leaked in exception {field}: "
                    f"{str(value)[:160]}"
                )

    # Also confirm redaction marker appears in at least one structured record.
    full_blob = captured_blob
    if "***" not in full_blob and "[REDACTED]" not in full_blob:
        return False, (
            "no redaction marker (*** or [REDACTED]) found in emitted JSONL records"
        )

    print(f"  records: {len(records)}; normal fields preserved")
    _print_pass("redaction")
    return True, ""


# ───────────────────── Scenario C: Production Guard ─────────────────────────
PROD_CHILD_SCRIPT = textwrap.dedent(
    """
    import json, logging, sys
    from pathlib import Path

    from app.core.logging import (
        LoggingSettings,
        bind_log_context,
        clear_log_context,
        log_event,
        setup_logging,
    )

    log_dir = Path(sys.argv[1])
    settings = LoggingSettings(
        environment="production",
        console_enabled=True,
        file_enabled=True,  # intentionally set; guard must override
        log_dir=log_dir,
    )
    setup_logging(settings)

    logger = logging.getLogger("testagent.application")
    bind_log_context(task_id="task_logging_acceptance_production")
    log_event(logger, logging.INFO, "application.test.info", "production smoke info")
    log_event(
        logger,
        logging.WARNING,
        "application.test.warning",
        "production smoke warning with api_key=LOG_ACCEPTANCE_PRODUCTION_SECRET_123",
    )
    clear_log_context()
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass
    sys.stdout.flush()
    sys.stderr.flush()
    """
).strip()


def run_production_guard_acceptance(*, artifact_root: Path) -> tuple[bool, str]:
    log_dir = artifact_root / "production"
    log_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["APP_ENV"] = "production"
    env["LOG_FILE_ENABLED"] = "1"   # intentionally wrong
    env["LOG_FORMAT"] = "json"      # honor the contract
    env["LOG_DIR"] = str(log_dir)   # subprocess writes here — never ./logs
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONPATH", ".")

    result = subprocess.run(
        [sys.executable, "-c", PROD_CHILD_SCRIPT, str(log_dir)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )

    if result.returncode != 0:
        return False, (
            f"production child process exit {result.returncode}; "
            f"stderr={result.stderr[:400]!r}"
        )

    # Identify TestAgent structured records on stdout (filter out warnings,
    # pip noise, etc. — we only need our own JSON lines).
    testagent_records: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("service") == "testagent-backend":
            testagent_records.append(obj)

    if not testagent_records:
        return False, (
            "no TestAgent structured JSON record found on stdout; "
            f"stdout sample: {result.stdout[:300]!r}"
        )

    # required fields
    sample = testagent_records[0]
    for required in ("timestamp", "level", "service", "environment", "event", "message"):
        if required not in sample:
            return False, (
                f"missing required field {required!r} in production JSON: {sample}"
            )
    if str(sample.get("environment")).lower() != "production":
        return False, (
            f"environment is not production in production mode: {sample.get('environment')!r}"
        )

    # Redaction: ensure the sentinel does not leak.
    blob = json.dumps(testagent_records, ensure_ascii=False)
    if SENTINEL_PRODUCTION_SECRET in blob:
        return False, "sentinel secret leaked into production stdout JSON"

    # File guard: confirm app JSONL files were NOT created.
    app_dir = log_dir / "app"
    error_dir = log_dir / "error"
    forbidden = list(app_dir.glob("testagent*.jsonl")) + list(error_dir.glob("testagent*.jsonl"))
    if forbidden:
        return False, (
            "production guard failed: JSONL files were created under "
            f"{log_dir}: {[p.name for p in forbidden]}"
        )

    # Success markers we should see in stderr.
    if "[testagent logging] LOG_FILE_ENABLED ignored in production" not in result.stderr:
        return False, (
            "expected stderr banner '[testagent logging] LOG_FILE_ENABLED ignored in production' "
            f"missing. stderr={result.stderr[:300]!r}"
        )

    print(f"  structured records on stdout: {len(testagent_records)}")
    print(f"  environment={sample.get('environment')}")
    _print_pass("production-guard")
    return True, ""


# ─────────────────────── Orchestration ──────────────────────────────────────
# ─────────────────── Scenario D: Error Response Correlation ─────────────────
def _error_correlation_app():
    """Build the FastAPI app used by both error-response scenarios."""
    from fastapi import FastAPI, HTTPException
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    from app.core.logging import (
        RequestLoggingMiddleware,
        bind_log_context,
        get_request_correlation,
    )

    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.exception_handler(Exception)
    async def _fallback(request, exc):  # type: ignore[no-untyped-def]
        request_id, trace_id = get_request_correlation(request)
        bind_log_context(request_id=request_id, trace_id=trace_id)
        logging.getLogger("testagent.application").exception(
            "fallback fired | path=%s", request.url.path
        )
        response = JSONResponse({"code": 50001, "message": "server error"}, status_code=500)
        if request_id:
            response.headers["X-Request-ID"] = request_id
        return response

    @app.get("/logging-acceptance/echo-200")
    async def echo_200():
        return {"ok": True}

    @app.get("/logging-acceptance/not-found")
    async def not_found():
        raise HTTPException(status_code=404, detail="not here")

    @app.get("/logging-acceptance/forbidden")
    async def forbidden():
        raise HTTPException(status_code=403, detail="denied")

    @app.get("/logging-acceptance/validation-error")
    async def validation_error(value: int):
        return {"value": value}

    @app.get("/logging-acceptance/boom-500")
    async def boom_500():
        raise RuntimeError(
            "LOG_ACCEPTANCE_CONTROLLED_EXCEPTION — error-response correlation test"
        )

    return app


def run_error_response_correlation(*, artifact_root: Path) -> tuple[bool, str]:
    """Global exception ERROR must carry request_id (Known Limitation 1)."""
    import httpx
    from app.core.logging import (
        LoggingSettings,
        LogEvent,
        bind_log_context,
        clear_log_context,
        setup_logging,
    )

    log_dir = artifact_root
    log_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(
        LoggingSettings(
            environment="development",
            console_enabled=False,
            file_enabled=True,
            log_dir=log_dir,
            file_max_bytes=1024 * 1024,
            file_backup_count=1,
        )
    )

    app = _error_correlation_app()

    async def _drive() -> dict[str, str]:
        captured: dict[str, str] = {}
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/logging-acceptance/boom-500")
            captured["boom_rid"] = r.headers.get("X-Request-ID", "")
        return captured

    try:
        captured = asyncio_run(_drive())
    finally:
        clear_log_context()
    for handler in logging.getLogger().handlers:
        handler.flush()

    boom_rid = captured.get("boom_rid", "")
    if not boom_rid.startswith("req_"):
        return False, f"boom request_id missing or invalid: {boom_rid!r}"

    records = _all_records(log_dir)
    if not records:
        return False, f"no JSONL records produced under {log_dir}"

    error_records = [r for r in records if r.get("level") == "ERROR"]
    if not error_records:
        return False, "no ERROR records captured for the boom request"

    # Every ERROR record MUST carry request_id == boom_rid.
    missing = [
        r for r in error_records
        if str(r.get("request_id")) != boom_rid
    ]
    if missing:
        return False, (
            "ERROR records missing request_id; sampled: "
            f"{[(r.get('event'), r.get('request_id')) for r in error_records]}"
        )

    # Specifically: the global fallback ERROR must carry request_id.
    fallback_errors = [
        r for r in error_records
        if "fallback" in str(r.get("message", "")).lower()
        or "未处理" in str(r.get("message", ""))
    ]
    if not fallback_errors:
        return False, (
            f"no global fallback ERROR identified; messages: "
            f"{[r.get('message') for r in error_records]}"
        )
    for record in fallback_errors:
        if str(record.get("request_id")) != boom_rid:
            return False, (
                f"global fallback ERROR missing request_id: req={record.get('request_id')} rid={boom_rid}"
            )

    print(f"  boom request_id={boom_rid}")
    print(f"  ERROR records: {len(error_records)}; fallback errors: {len(fallback_errors)}")
    _print_pass("error-response-correlation")
    return True, ""


# ───────────── Scenario E: Error Response X-Request-ID Header ──────────────
def run_error_response_request_id_header(*, artifact_root: Path) -> tuple[bool, str]:
    """Every HTTP response (2xx/4xx/422/5xx) carries X-Request-ID."""
    import httpx
    from app.core.logging import (
        LoggingSettings,
        bind_log_context,
        clear_log_context,
        setup_logging,
    )

    log_dir = artifact_root
    log_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(
        LoggingSettings(
            environment="development",
            console_enabled=False,
            file_enabled=True,
            log_dir=log_dir,
            file_max_bytes=1024 * 1024,
            file_backup_count=1,
        )
    )

    app = _error_correlation_app()

    cases: list[tuple[str, int]] = [
        ("/logging-acceptance/echo-200", 200),
        ("/logging-acceptance/not-found", 404),
        ("/logging-acceptance/forbidden", 403),
        ("/logging-acceptance/validation-error?value=notanint", 422),
        ("/logging-acceptance/boom-500", 500),
    ]

    async def _drive() -> list[tuple[str, int, str]]:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        results: list[tuple[str, int, str]] = []
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path, expected_status in cases:
                resp = await client.get(path)
                results.append((path, resp.status_code, resp.headers.get("X-Request-ID", "")))
        return results

    try:
        results = asyncio_run(_drive())
    finally:
        clear_log_context()
    for handler in logging.getLogger().handlers:
        handler.flush()

    for path, status, rid in results:
        if not rid.startswith("req_"):
            return False, f"{path} (status={status}) missing X-Request-ID: {rid!r}"

    # Each header must appear in the JSONL records (so operators can
    # query --request-id and find the matching lifecycle log).
    records = _all_records(log_dir)
    if not records:
        return False, f"no JSONL records produced under {log_dir}"

    seen_request_ids: set[str] = {
        str(r.get("request_id")) for r in records
        if r.get("request_id")
    }
    for _path, _status, rid in results:
        if rid not in seen_request_ids:
            return False, (
                f"header request_id {rid!r} has no matching JSONL record"
            )

    print(f"  cases: {len(results)}; all carry X-Request-ID and match JSONL")
    for path, status, rid in results:
        print(f"    {status:>3}  {path}  rid={rid[:24]}…")
    _print_pass("error-response-request-id-header")
    return True, ""


SCENARIOS = {
    "exception": ("Exception Chain + Correlation", run_exception_acceptance),
    "redaction": ("Redaction Pipeline", run_redaction_acceptance),
    "production-guard": ("Production Guard", run_production_guard_acceptance),
    "error-response-correlation": ("Error Response Correlation", run_error_response_correlation),
    "error-response-request-id-header": ("Error Response X-Request-ID Header", run_error_response_request_id_header),
}


def _teardown_owned_handlers() -> None:
    """Close every TestAgent-owned logging handler so Windows can clean tmpdir."""
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


def _persist_artifacts(tmp_root: Path, scratch_root: Path) -> list[Path]:
    """Mirror the temporary log dir into backend/tmp/logging_acceptance for diagnostics."""
    if not tmp_root.exists():
        return []
    saved: list[Path] = []
    for child in tmp_root.iterdir():
        target = scratch_root / child.name
        if child.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for f in child.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(child)
                    dest = target / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(f.read_bytes())
                    saved.append(dest)
    return saved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run TestAgent logging acceptance scenarios.")
    parser.add_argument(
        "scenario",
        choices=tuple(SCENARIOS) + ("all",),
        nargs="?",
        default="all",
        help="Which acceptance scenario to run (default: all)",
    )
    args = parser.parse_args(argv)

    selected: list[str] = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results: dict[str, tuple[bool, str]] = {}
    scratch_root = _scratch_dir()

    print("Logging Acceptance Harness")
    print(f"  scenarios: {', '.join(selected)}")
    print(f"  scratch : {scratch_root}")
    print()

    with tempfile.TemporaryDirectory(prefix="logging_acceptance_") as tmp_root_str:
        tmp_root = Path(tmp_root_str)
        for name in selected:
            label, runner = SCENARIOS[name]
            print(f"── scenario: {label}")
            artifact_root = tmp_root / name
            artifact_root.mkdir(parents=True, exist_ok=True)
            try:
                ok, reason = runner(artifact_root=artifact_root)
            except Exception as exc:  # noqa: BLE001
                ok = False
                reason = f"unexpected exception: {exc!r}\n{traceback.format_exc(limit=8)}"
            if not ok:
                saved = _persist_artifacts(artifact_root, scratch_root / name)
                _print_fail(name, reason, "", saved)
            results[name] = (ok, reason)
            print()
        # Tear down owned handlers so the temporary directory can be
        # cleaned up on Windows (open file handles block rmtree).
        _teardown_owned_handlers()

    # Summary block.
    print("Logging Acceptance Summary")
    print()
    any_fail = False
    for name in selected:
        ok, _ = results[name]
        verdict = "PASS" if ok else "FAIL"
        print(f"  {name}: {verdict}")
        if not ok:
            any_fail = True
    print()
    if any_fail:
        print("OVERALL: FAIL")
    else:
        print("OVERALL: PASS")
    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
