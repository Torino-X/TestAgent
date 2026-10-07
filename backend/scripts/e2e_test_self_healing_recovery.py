#!/usr/bin/env python3
"""F023 Step 7 — End-to-end auto-recovery against a real LLM.

Drives the full orchestrator self-healing loop on a real
``TestPlanGeneratorTool`` run (no MockLLMClient, no silent fallback).
Loads the LLM config from the dev ``model_configs`` row bound to the
``e2e_test`` user (id=10) so we use a real API key — and the real
``SettingsService.build_llm_config_provider`` path.

The first attempt is **deliberately corrupted** by wrapping
``ResultParser.parse_and_validate_json`` so it renames one AI field
on attempt 1 (e.g. ``section_1`` → ``__WRONG_FIELD_NAME__``). The
validator refuses anything that isn't ``section_1`` and raises
``ResultParseError``. The tool catches this, returns
``JSON_VALIDATION_FAILED`` recoverable=True, and the orchestrator
classifies it via ``RetryPolicy`` → ``schema_feedback`` strategy →
RETRIES with a RetryContext carrying ``strategy=="schema_feedback"``
and the previous error message.

The tool's gated internal corrective block
(``_maybe_retry_with_schema_fix``) sees ``retry_context.strategy ==
"schema_feedback"`` and re-prompts the real LLM with an explicit
"use these EXACT field names" block. On attempt 2 the LLM returns
correct field names, the real ``parse_and_validate_json`` accepts it,
and the tool returns success.

This is the user-visible "字段名错误 → 自动重试 + 成功" recovery flow.

Asserts:
  * ≥ 2 attempts (1 initial + ≥ 1 retry)
  * ≥ 1 RETRYING SSE event with ``strategy == "schema_feedback"``
  * RetryContext on attempt ≥ 2 carries ``strategy ==
    "schema_feedback"`` AND the previous error message
  * Final tool result is success=True with valid section_package
  * All required AI fields restored after recovery (no more
    ``__WRONG_FIELD_NAME__`` in payload)
  * No API key leaks anywhere

Usage:
    $ python backend/scripts/e2e_test_self_healing_recovery.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _PROJECT_ROOT / "backend"
sys.path.insert(0, str(_BACKEND_DIR))

# Load .env (DATABASE_SYNC_URL etc.)
from dotenv import load_dotenv  # noqa: E402
_env_file = _BACKEND_DIR / ".env"
if _env_file.exists():
    load_dotenv(_env_file)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("e2e_self_healing")

from app.common.result_parser import (  # noqa: E402
    ResultParser,
    ResultParseError,
    JsonValidationResult,
)


def mask(s: str, keep: int = 4) -> str:
    if not s:
        return "[MISSING]"
    if len(s) <= keep * 2 + 2:
        return s[:2] + "****" + s[-2:]
    return s[:keep] + "****" + s[-keep:]


def print_section(title: str) -> None:
    print()
    print("-" * 74)
    print(f"  {title}")
    print("-" * 74)
    print()


def print_check(label: str, passed: bool, *, hint: str = "") -> bool:
    status = "OK" if passed else "FAIL"
    line = f"  [{status}] {label}"
    if hint:
        line += f"   ({hint})"
    print(line)
    return passed


# ════════════════════════════════════════════════════════════════════
# Attempt-1 poison wrapper
# ════════════════════════════════════════════════════════════════════


class _AttemptOnceResultParser:
    """Wrap ``ResultParser.parse_and_validate_json`` so attempt #1
    always renames one AI field. Attempt ≥ 2 delegates to the real
    parser so the orchestrator can observe a genuine auto-recovery.

    Wrapping strategy:
      1. ``parse_and_validate_json(content, config)`` → calls the real
         ``parse_and_validate_json`` which returns a clean validated
         payload dict.
      2. We rename one AI field on attempt 1, then re-run the real
         ``validate_json_payload`` so the validator raises a
         ``ResultParseError`` for the simulated misnaming.

    This way, the validator's existing ``required_fields`` /
    ``extra_fields`` check fires naturally, and the tool + retry
    policy see a true validation failure (rather than a payload that
    just happens to look wrong).
    """

    def __init__(self, parse_validate_fn, validate_payload_fn, poison_field: str):
        self._parse_validate = parse_validate_fn
        self._validate_payload = validate_payload_fn
        self._poison_field = poison_field
        self.attempts: int = 0

    def __call__(self, content: str, config):
        self.attempts += 1
        # Step 1: real parse + validate (with full unmodified dict)
        payload = self._parse_validate(content, config)

        if self.attempts == 1:
            logger.warning(
                "E2E poison: attempt #1 received fields=%s (config "
                "required=%s)",
                sorted(payload.keys()),
                [
                    f.get("field") if isinstance(f, dict) else f
                    for f in (config or {}).get("ai_fields", [])
                ],
            )
            # Step 2: rename one AI field on attempt 1
            renamed = dict(payload)
            if self._poison_field in renamed:
                renamed["__WRONG_FIELD_NAME__"] = renamed.pop(
                    self._poison_field
                )
            else:
                ai_fields = (config or {}).get("ai_fields", [])
                if ai_fields:
                    target = ai_fields[0].get("field") or self._poison_field
                    if target in renamed:
                        renamed["__WRONG_FIELD_NAME__"] = renamed.pop(target)

            logger.warning(
                "E2E poison: attempt #1 final fields=%s",
                sorted(renamed.keys()),
            )

            # Step 3: re-validate. The real validator returns a result
            # (not raising); we lift missing/extra/schema-mismatch into
            # a real ResultParseError so the tool + retry policy see the
            # genuine validation failure the user would see in prod.
            result = self._validate_payload(renamed, config)
            if not result.passed:
                problems: list[str] = []
                if result.missing_fields:
                    problems.append("缺少字段：" + "、".join(result.missing_fields))
                if result.extra_fields:
                    problems.append(
                        "不允许的字段：" + "、".join(result.extra_fields)
                    )
                if result.empty_fields:
                    problems.append(
                        "内容为空字段：" + "、".join(result.empty_fields)
                    )
                if result.schema_mismatch_fields:
                    problems.append(
                        "结构不符合模板字段："
                        + "；".join(result.schema_mismatch_fields)
                    )
                raise ResultParseError(
                    "模型 JSON 输出校验失败，" + "；".join(problems)
                )
            return renamed

        # Subsequent attempts: pure pass-through.
        return payload


# ════════════════════════════════════════════════════════════════════
# DB-backed SettingsService that resolves the e2e_test user's config
# ════════════════════════════════════════════════════════════════════


def _build_db_settings_service(session, user_internal_id: int):
    """Wrap the real SettingsService around a DB-backed session.

    The real SettingsService.build_llm_config_provider already does
    LLMConfigCache.get_or_load(user_id, loader), which lazily decrypts
    the API key on first miss and caches it.  We just pass the same
    service through so the orchestrator + Tool paths see the same
    code-shape as production login → bootstrap flow.
    """
    from app.services.settings_service import SettingsService
    real_service = SettingsService(session)

    class _DBService:
        def __getattr__(self, name):
            return getattr(real_service, name)

    return _DBService()


# ════════════════════════════════════════════════════════════════════
# SSE recorder
# ════════════════════════════════════════════════════════════════════


@dataclass
class _RecordedEvent:
    event_type: str
    title: str
    content: str = ""
    payload: dict | None = None


def _install_recorder(orch) -> list[_RecordedEvent]:
    recorder: list[_RecordedEvent] = []

    async def fake_publish(self, ctx, event_type, title, content="", payload=None):
        recorder.append(
            _RecordedEvent(
                event_type=str(event_type),
                title=title,
                content=content,
                payload=payload,
            )
        )

    orch._publish = fake_publish.__get__(orch, type(orch))  # bound method
    return recorder


@contextmanager
def _patch_repos():
    """Stub DB repositories so orchestrator persistence calls are no-ops."""
    from app.repositories import event_repository, tool_call_repository

    class _FakeEventRepo:
        def __init__(self, _session):
            pass

        async def create(self, _row):
            return SimpleNamespace(public_id="evt_recover")

    class _FakeToolCallRepo:
        def __init__(self, _session):
            pass

        async def create(self, _row):
            return SimpleNamespace(public_id="tc_recover")

    old_e = event_repository.EventRepository
    old_t = tool_call_repository.ToolCallRepository
    event_repository.EventRepository = _FakeEventRepo  # type: ignore[assignment]
    tool_call_repository.ToolCallRepository = _FakeToolCallRepo  # type: ignore[assignment]
    try:
        yield
    finally:
        event_repository.EventRepository = old_e
        tool_call_repository.ToolCallRepository = old_t


# ════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════


async def main() -> int:
    print("=" * 74)
    print("  F023 Step 7 -- Real-LLM Self-Healing Auto-Recovery E2E")
    print("=" * 74)

    # ── 0. Resolve user ───────────────────────────────────────────
    print_section("Phase 0: Resolve e2e_test user + model_config")

    E2E_USERNAME = os.getenv("E2E_USERNAME", "e2e_test")

    from sqlalchemy import select
    from app.db.session import AsyncSessionLocal
    from app.models.user import User
    from app.models.config import ModelConfig

    async with AsyncSessionLocal() as session:
        user = (await session.execute(
            select(User).where(User.username == E2E_USERNAME)
        )).scalar_one_or_none()
        if user is None:
            print(f"  ABORT: user {E2E_USERNAME!r} not found in dev DB")
            return 2
        print(f"  User: id={user.id} username={user.username} role={user.role}")

        cfg = (await session.execute(
            select(ModelConfig).where(ModelConfig.user_id == user.id)
        )).scalar_one_or_none()
        if cfg is None:
            print(f"  ABORT: user {E2E_USERNAME!r} has no ModelConfig row")
            return 2
        api_url_mask = cfg.api_base_url
        model_name = cfg.model_name
        # Decrypt the key ONLY for the masked-print (never log full key)
        from app.core.crypto import decrypt_api_key
        try:
            api_key = decrypt_api_key(cfg.api_key_encrypted)
        except Exception:
            api_key = ""
        print(f"  Config: id={cfg.id} model={model_name} url={api_url_mask}")
        print(f"  API Key (masked) = {mask(api_key)}")
        print(f"  timeout_seconds  = {cfg.timeout_seconds}s")
        print(f"  enable_thinking  = {bool(cfg.enable_thinking)}")

    # ── 1. Build input files ──────────────────────────────────────
    print_section("Phase 1: Build requirement + template docx")
    from docx import Document
    from docx.shared import Pt
    from app.storage.local_storage import local_storage

    uploads_dir = local_storage._base / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:8]

    req_filename = f"e2e_recover_req_{run_id}.docx"
    req_path = uploads_dir / req_filename
    req_doc = Document()
    req_doc.styles["Normal"].font.size = Pt(12)
    req_doc.add_heading("智慧校园综合服务平台 -- 需求文档", 0)
    req_doc.add_paragraph(
        "系统包含学生登录、课程管理、通知公告三个核心模块。"
        "请覆盖功能测试、权限校验、异常场景三类用例。"
    )
    req_doc.save(str(req_path))
    print(f"  Requirement: {req_filename} ({req_path.stat().st_size} bytes)")

    tpl_filename = f"e2e_recover_tpl_{run_id}.docx"
    tpl_path = uploads_dir / tpl_filename
    tpl_doc = Document()
    tpl_doc.styles["Normal"].font.size = Pt(12)
    tpl_doc.add_heading("1. 项目概述", level=1)
    tpl_doc.add_paragraph("填写项目背景和目标概述").italic = True
    tpl_doc.add_heading("2. 测试范围", level=1)
    tpl_doc.add_paragraph("说明本次测试覆盖的功能范围").italic = True
    tpl_doc.save(str(tpl_path))
    print(f"  Template:    {tpl_filename} ({tpl_path.stat().st_size} bytes)")

    # ── 2. Set up AgentContext with DB-backed settings_service ───
    print_section("Phase 2: Build AgentContext with DB-backed settings_service")

    from app.agent.context import AgentContext

    async with AsyncSessionLocal() as session:
        ctx = AgentContext(
            task_id=f"task_recover_{run_id}",
            conversation_id=f"conv_recover_{run_id}",
            user_id=user.public_id,
            user_internal_id=user.id,
            conversation_internal_id=user.id,  # fake; not needed for this run
            task_internal_id=1,
            session=session,
            requirement_file_id=req_filename,
            template_file_id=tpl_filename,
            settings_service=_build_db_settings_service(session, user.id),
        )

        # ── 3. Run RequirementParser + TemplateParser ────────────
        print_section("Phase 3: RequirementParser + TemplateParser (real)")

        from app.tools.requirement_parser_tool import RequirementParserTool
        from app.tools.template_parser_tool import TemplateParserTool

        req_result = await RequirementParserTool().run(
            {"requirement_file_id": req_filename}, ctx
        )
        if not req_result["success"]:
            print(f"  FAIL RequirementParser: {req_result.get('error')}")
            return 3
        print(
            f"  OK requirement_analysis.text_content="
            f"{len(ctx.requirement_analysis.get('text_content', ''))} chars"
        )

        tpl_result = await TemplateParserTool().run(
            {"template_file_id": tpl_filename}, ctx
        )
        if not tpl_result["success"]:
            print(f"  FAIL TemplateParser: {tpl_result.get('error')}")
            return 3
        ai_fields = ctx.template_structure.get(
            "generation_config", {}
        ).get("ai_fields", [])
        if not ai_fields:
            print("  FAIL: template has no AI fields.")
            return 3
        first_field = ai_fields[0].get("field") or "section_1"
        print(
            f"  OK template parsed: {len(ai_fields)} AI fields. "
            f"Will poison first field: '{first_field}'"
        )

        # ── 4. Install poison wrapper on attempt #1 ──────────────
        print_section("Phase 4: Install poison wrapper on ResultParser")

        from app.tools import test_plan_generator_tool as tpg_module

        parser = ResultParser()
        wrapper = _AttemptOnceResultParser(
            parse_validate_fn=parser.parse_and_validate_json,
            validate_payload_fn=parser.validate_json_payload,
            poison_field=first_field,
        )
        original_parser_class = tpg_module.ResultParser

        def _wrapped_parser_cls(*_a, **_kw):
            inst = original_parser_class(*_a, **_kw)
            inst.parse_and_validate_json = (
                lambda content, config=None: wrapper(
                    content, config or {}
                )
            )
            # Diagnostic: capture raw + result of each attempt so we
            # can see what the validator saw.
            inst._e2e_attempt_count = lambda: wrapper.attempts
            return inst

        tpg_module.ResultParser = _wrapped_parser_cls  # type: ignore[assignment]
        print(
            f"  OK wrapper installed: attempt 1 renames '{first_field}' "
            f"→ '__WRONG_FIELD_NAME__'; attempt ≥2 delegates to real parser"
        )

        # ── 5. Drive orchestrator ────────────────────────────────
        print_section("Phase 5: Drive AgentOrchestrator._run_tool_with_retry")

        from app.agent.orchestrator import AgentOrchestrator
        from app.tools.register import register_all_tools

        # The module-level tool_registry is normally populated on app
        # startup.  For a standalone E2E script we register explicitly.
        register_all_tools()

        orch = AgentOrchestrator()
        recorder = _install_recorder(orch)

        t0 = time.monotonic()
        with _patch_repos():
            gen_result = await orch._run_tool_with_retry(
                ctx, "TestPlanGeneratorTool",
                {"use_mock": False, "user_prompt": "请根据模板生成测试方案。"},
            )
        total_latency = (time.monotonic() - t0) * 1000.0
        print(f"  Total latency: {total_latency:.1f} ms")
        print(f"  Tool success:  {gen_result['success']}")
        if not gen_result["success"]:
            err = gen_result.get("error", {})
            print(f"  Error: {err.get('code')} -- {err.get('message', '')[:200]}")

        # Restore module-level class so we don't leak monkey-patch
        tpg_module.ResultParser = original_parser_class  # type: ignore[assignment]

        # ── 6. Assertions ────────────────────────────────────────
        print_section("Phase 6: Verify self-healing sequence")

        checks_ok = True
        started_count = sum(
            1 for e in recorder if e.event_type == "tool_started"
        )
        checks_ok &= print_check(
            "6a: ≥2 attempts (initial + retries)",
            started_count >= 2,
            hint=f"started={started_count}",
        )

        retrying_events = [e for e in recorder if e.event_type == "retrying"]
        has_schema_feedback = any(
            (e.payload or {}).get("strategy") == "schema_feedback"
            for e in retrying_events
        )
        strategies_seen = [
            (e.payload or {}).get("strategy") for e in retrying_events
        ]
        checks_ok &= print_check(
            "6b: ≥1 RETRYING event with strategy=schema_feedback",
            has_schema_feedback,
            hint=f"retry_events={len(retrying_events)} strategies={strategies_seen}",
        )

        checks_ok &= print_check(
            "6c: final result.success=True",
            bool(gen_result.get("success")),
            hint=f"code={(gen_result.get('error') or {}).get('code', '')}",
        )

        gen_data = (
            (gen_result.get("data") or {})
            if gen_result.get("success") else {}
        )
        sp = (
            gen_data.get("section_package", {})
            if isinstance(gen_data, dict) else {}
        )
        checks_ok &= print_check(
            "6d: section_package built",
            bool(sp) and isinstance(sp, dict),
        )

        required_fields = [
            f.get("field") for f in ai_fields if f.get("field")
        ]
        payload_inner = (
            sp.get("payload", {}) if isinstance(sp, dict) else {}
        )
        missing_required = [f for f in required_fields if f not in payload_inner]
        # Also check that the sentinel "__WRONG_FIELD_NAME__" is gone:
        leaked_sentinel = "__WRONG_FIELD_NAME__" in payload_inner
        checks_ok &= print_check(
            "6e: required AI fields restored (no '__WRONG_FIELD_NAME__' "
            "in payload)",
            (not leaked_sentinel) and (len(missing_required) == 0),
            hint=(
                f"missing={missing_required} "
                f"leaked_sentinel={leaked_sentinel}"
            ),
        )

        checks_ok &= print_check(
            "6f: context.test_plan_content set",
            ctx.test_plan_content is not None,
        )

        if retrying_events:
            first_payload = retrying_events[0].payload or {}
            last_error_text = first_payload.get("last_error", "")
            checks_ok &= print_check(
                "6g: RETRYING payload carries last_error",
                len(last_error_text) > 0,
                hint=f"len(last_error)={len(last_error_text)}",
            )
            checks_ok &= print_check(
                "6h: RETRYING payload has tool_name",
                bool(first_payload.get("tool_name")),
            )

        # Leak check — also verify the full retry sequence doesn't
        # leak the API key.
        output_str = json.dumps(
            {
                "recorder": [
                    {"type": e.event_type, "title": e.title,
                     "payload": e.payload, "content": e.content}
                    for e in recorder
                ],
                "result": gen_result,
            },
            ensure_ascii=False, default=str,
        )
        checks_ok &= print_check(
            "6i: API key never appears in any SSE payload or result",
            api_key not in output_str,
            hint="check API key not leaked",
        )

        # ── 7. Per-attempt timing log ────────────────────────────
        print_section("Phase 7: Per-attempt event log")
        # Print TOOL_STARTED / TOOL_FAILED / TOOL_FINISHED in order
        # so we can see the rate of attempts.
        idx = 0
        for ev in recorder:
            if ev.event_type in (
                "tool_started", "tool_finished", "tool_failed", "retrying"
            ):
                payload_keys = (
                    list(ev.payload.keys())
                    if ev.payload else None
                )
                title = ev.title[:60]
                print(
                    f"  [{idx:02d}] {ev.event_type:14s} {title:60s}  "
                    f"payload_keys={payload_keys}"
                )
                idx += 1

        # ── 8. Verdict ───────────────────────────────────────────
        print()
        print("=" * 74)
        if checks_ok:
            print("  VERDICT: PASS — orchestrator recovered from real "
                  "LLM field-naming error")
        else:
            print("  VERDICT: FAIL — see checks above")
        print("=" * 74)
        print(f"  Run ID:        {run_id}")
        print(f"  Total latency: {total_latency:.0f} ms")
        print(f"  Attempts:      {started_count}")
        print(f"  RETRYING evts: {len(retrying_events)}")
        print(f"  Final success: {bool(gen_result.get('success'))}")
        print()

    # Cleanup temp files
    for p in [req_path, tpl_path]:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass

    return 0 if checks_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
