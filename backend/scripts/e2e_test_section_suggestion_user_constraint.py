#!/usr/bin/env python3
"""F022 Step 4 — End-to-end SectionSuggestionTool user-constraint flow.

Drives the real :class:`SectionSuggestionTool` with a real LLM
(qwen3.7-plus from the dev ``model_configs`` row bound to
``e2e_test`` user id=10).  Uses the actual
``SettingsService.build_llm_config_provider`` path — no mock client,
no silent fallback.

Pipeline:
  1. Load ``model_configs`` row for user_id=10.
  2. Build a minimal ``template_structure`` (3 sections) and an
     ``AgentContext`` populated with a ``user_prompt`` that mentions
     one of the sections by name.
  3. Call ``SectionSuggestionTool.run`` directly.
  4. Assert:
       a. tool returns success=True
       b. ctx.section_suggestions was populated
       c. The mentioned section got ``constraint_source="user_prompt"``
       d. The override action matches what the user requested
       e. Untouched sections keep their template-mode default
       f. ctx.user_constraints echoes what was applied
       g. No API key appears anywhere in the tool output / log

Usage:
    $ python backend/scripts/e2e_test_section_suggestion_user_constraint.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _PROJECT_ROOT / "backend"
sys.path.insert(0, str(_BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402
_env_file = _BACKEND_DIR / ".env"
if _env_file.exists():
    load_dotenv(_env_file)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("e2e_user_constraint")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.agent.context import AgentContext  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.settings_service import SettingsService  # noqa: E402
from app.tools.section_suggestion_tool import SectionSuggestionTool  # noqa: E402


USER_INTERNAL_ID = 10  # dev e2e_test user — already has model_configs


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


def assert_no_api_key(obj, path: str = "") -> bool:
    """Walk a JSON-ish structure and bail on any string that looks like a
    DashScope API key (starts with ``sk-`` and is ≥ 20 chars long)."""
    if isinstance(obj, dict):
        return all(assert_no_api_key(v, f"{path}.{k}") for k, v in obj.items())
    if isinstance(obj, list):
        return all(assert_no_api_key(v, f"{path}[{i}]") for i, v in enumerate(obj))
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("sk-") and len(s) >= 20 and "****" not in s:
            print(f"  [FAIL] API key leaked at {path}: {mask(s)}")
            return False
    return True


async def _resolve_provider(session: AsyncSession, user_internal_id: int):
    settings = SettingsService(session)
    provider = await settings.build_llm_config_provider(user_id=user_internal_id)
    if not provider or getattr(provider, "is_unconfigured", False):
        raise RuntimeError(
            f"e2e_test user (id={user_internal_id}) has no model_config; "
            f"seed_dev_data.py should have populated it.",
        )
    api_key = getattr(provider, "api_key", "") or ""
    print(
        f"  LLM provider resolved | model={getattr(provider, 'model_name', '?')} "
        f"| api_key={mask(api_key)}",
    )
    return settings, provider


async def main() -> int:
    print_section("F022 Step 4 — SectionSuggestionTool real-LLM user-constraint E2E")
    failed_checks: list[str] = []

    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(
                select(User).where(User.id == USER_INTERNAL_ID),
            )
        ).scalar_one_or_none()
        if user is None:
            print(f"  [FATAL] user id={USER_INTERNAL_ID} not found")
            return 1

        settings, provider = await _resolve_provider(session, user.id)

        # ── Build a minimal template structure ───────────────────────
        template_structure = {
            "sections": [
                {
                    "section_id": "sec_overview",
                    "title": "项目概述",
                    "level": 0,
                    "mode": "ai",
                },
                {
                    "section_id": "sec_equipment",
                    "title": "测试设备",
                    "level": 0,
                    "mode": "manual",
                },
                {
                    "section_id": "sec_schedule",
                    "title": "测试进度",
                    "level": 0,
                    "mode": "ai",
                },
            ],
        }

        # User prompt asks for "测试设备" to be kept verbatim
        user_prompt = (
            "请按智慧校园综合服务平台需求生成测试方案。"
            "其中「测试设备」章节保留模板原文，不要用 AI 生成。"
        )

        ctx = AgentContext(
            task_id="e2e_f022_task",
            conversation_id="e2e_f022_conv",
            user_id="user_e2e",
            user_internal_id=user.id,
            settings_service=settings,
        )
        ctx.template_structure = template_structure
        ctx.user_prompt = user_prompt

        # ── Run the tool ─────────────────────────────────────────────
        print_section("Step A — invoke SectionSuggestionTool")
        tool = SectionSuggestionTool()
        result = await tool.run({}, ctx)
        print(f"  result.success     = {result.get('success')}")
        print(f"  result.summary     = {result.get('summary', '')[:200]}")
        print(f"  sections count     = {len(result.get('data', {}).get('sections', []))}")
        sections_out = result.get("data", {}).get("sections", []) or []
        for s in sections_out:
            print(
                f"    - {s['title']:<8} action={s.get('suggested_action'):<14} "
                f"source={s.get('constraint_source', 'template')} "
                f"reason={s.get('reason', '')[:60]}",
            )

        # ── Assertions ───────────────────────────────────────────────
        print_section("Step B — assertions")
        check_passed = True

        if not print_check("tool returned success=True", result.get("success") is True):
            check_passed = False
            failed_checks.append("success")
        if not print_check(
            "ctx.section_suggestions populated",
            ctx.section_suggestions is not None
            and ctx.section_suggestions.get("sections"),
        ):
            check_passed = False
            failed_checks.append("ctx.section_suggestions")
        if not print_check(
            "ctx.user_constraints populated",
            bool(getattr(ctx, "user_constraints", None)),
        ):
            check_passed = False
            failed_checks.append("ctx.user_constraints")
        else:
            print(f"    user_constraints keys: {sorted(ctx.user_constraints.keys())}")

        # Find the 测试设备 suggestion
        equipment_sections = [
            s for s in sections_out if s.get("title") == "测试设备"
        ]
        if not equipment_sections:
            print_check("'测试设备' section appears in output", False)
            check_passed = False
            failed_checks.append("equipment_missing")
        else:
            eq = equipment_sections[0]
            if not print_check(
                "'测试设备' override applied (constraint_source=user_prompt)",
                eq.get("constraint_source") == "user_prompt",
                hint=f"got {eq.get('constraint_source')!r}",
            ):
                check_passed = False
                failed_checks.append("override_source")
            if not print_check(
                "'测试设备' action flipped to keep_template",
                eq.get("suggested_action") == "keep_template",
                hint=f"got {eq.get('suggested_action')!r}",
            ):
                check_passed = False
                failed_checks.append("override_action")

        # Untouched sections must keep template defaults and NOT have constraint_source
        for title, expected_action in [
            ("项目概述", "ai_generate"),
            ("测试进度", "ai_generate"),
        ]:
            sec = next((s for s in sections_out if s.get("title") == title), None)
            if not print_check(
                f"'{title}' keeps default action {expected_action!r}",
                sec is not None and sec.get("suggested_action") == expected_action,
            ):
                check_passed = False
                failed_checks.append(f"default_{title}")
            if not print_check(
                f"'{title}' has no user-constraint override",
                sec is not None and "constraint_source" not in sec,
            ):
                check_passed = False
                failed_checks.append(f"override_{title}")

        # API-key leak guard
        if not assert_no_api_key(result, "result"):
            check_passed = False
            failed_checks.append("api_key_leak")

        print_section("Verdict")
        if check_passed:
            print("  VERDICT: PASS — SectionSuggestionTool real-LLM user-constraint flow works")
            print(f"  overrides applied: {len(getattr(ctx, 'user_constraints', {}) or {})}")
            return 0
        else:
            print("  VERDICT: FAIL")
            print(f"  failed checks: {failed_checks}")
            return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))