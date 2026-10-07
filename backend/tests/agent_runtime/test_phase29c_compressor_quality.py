"""Phase 2.9C compressor + quality gate tests."""

from __future__ import annotations

import pytest

from app.agent_runtime._shared.narrative_governance.compressor import (
    compress,
    measure_chars,
)
from app.agent_runtime._shared.narrative_governance.quality_validator import (
    sanitize_text,
    validate_and_repair,
)
from app.agent_runtime._shared.narrative_governance.schemas import (
    NarrativeContentBudget,
    NarrativeGovernanceContext,
)


def _budget(level: str) -> NarrativeContentBudget:
    return NarrativeContentBudget.for_level(level)


# ── Sanitizer ────────────────────────────────────────────────────


def test_sanitize_text_strips_bearer_tokens():
    text = "authorization: Bearer abc.def.ghi"
    sanitised = sanitize_text("authorization: Bearer abc.def.ghi")
    assert "Bearer" not in sanitised
    assert "[redacted]" in sanitised


def test_sanitize_text_strips_sk_keys():
    text = "provider key=" + "sk-" + "abcdefghijklmnop1234"
    sanitised = sanitize_text(text)
    assert "sk-abcdef" not in sanitised
    assert "[redacted]" in sanitised


def test_sanitize_text_strips_windows_path():
    sanitised = sanitize_text("see file at C:\\Users\\alice\\secret.txt")
    assert "[path]" in sanitised
    assert "Users\\alice" not in sanitised


def test_sanitize_text_strips_linux_path():
    sanitised = sanitize_text("read /home/alice/secret/.ssh/id_rsa")
    assert "[path]" in sanitised
    assert "alice" not in sanitised


def test_sanitize_text_strips_unc_path():
    sanitised = sanitize_text("file at \\\\server\\share\\file.txt")
    assert "[path]" in sanitised


def test_sanitize_text_strips_traceback():
    sanitised = sanitize_text(
        'Traceback (most recent call last):\n  File "x.py", line 12'
    )
    assert "Traceback" not in sanitised or "[traceback]" in sanitised


def test_sanitize_text_strips_dsn_password():
    sanitised = sanitize_text(
        "dialect=postgresql://user:supersecret@host/db"
    )
    assert "supersecret" not in sanitised
    assert "[redacted]" in sanitised


def test_sanitize_text_strips_jwt():
    sanitised = sanitize_text(
        "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSM"
    )
    assert "eyJ" not in sanitised


def test_sanitize_text_strips_url_query_token():
    sanitised = sanitize_text("https://x.com/api?token=abc123&keep=ok")
    assert "abc123" not in sanitised or "[redacted]" in sanitised


def test_sanitize_text_keeps_business_text_unchanged():
    text = "需求已收集,5条规则相关,准备进入下一步"
    sanitised = sanitize_text(text)
    assert sanitised == text


def test_sanitize_text_removes_chain_of_thought_markers():
    """CoT-style prompt injection markers are stripped from the public
    payload. The original phrase no longer appears after sanitisation.
    """
    sanitised = sanitize_text("we need to carefully evaluate before answer")
    assert "we need to" not in sanitised


# ── Compressor ────────────────────────────────────────────────────


def test_compress_drops_empty_fields():
    payload = {
        "headline": "ok",
        "summary": "x" * 400,
        "impact": "",
        "next_action": "",
        "details": [],
    }
    out = compress(payload, budget=_budget("standard"))
    assert out["headline"] == "ok"
    assert "…" in out["summary"]
    assert out["details"] == []


def test_compress_translates_internal_terminology():
    payload = {
        "headline": "ok",
        "summary": "task_internal_id error detected",
        "details": [],
    }
    out = compress(payload, budget=_budget("standard"))
    assert "task_internal_id" not in out["summary"]
    assert "任务标识" in out["summary"]


def test_compress_filters_details_by_allowlist():
    payload = {
        "headline": "ok", "summary": "x", "details": [],
    }
    out = compress(
        payload,
        budget=_budget("standard"),
        allowlist=["hit_count"],
    )
    assert out["details"] == []


def test_compress_handles_str_list_and_dict_details():
    payload = {
        "headline": "ok", "summary": "x",
        "details": ["a", "b", "c", "d", "e", "f"],
    }
    out = compress(payload, budget=_budget("standard"))
    assert isinstance(out["details"], list)
    assert len(out["details"]) <= 5  # 4 items + overflow marker


def test_measure_chars_empty_payload_returns_zero():
    assert measure_chars(None) == 0
    assert measure_chars({}) == 0


# ── Quality gate ────────────────────────────────────────────────


def _ctx(**kw) -> NarrativeGovernanceContext:
    base = {
        "task_id": "t1",
        "agent_name": "PreparationAgent",
        "update_kind": "agent_decision_update",
        "action": "call_tool",
    }
    base.update(kw)
    return NarrativeGovernanceContext.model_validate(base)


def test_validate_and_repair_strips_sensitive_tokens():
    candidate = {
        "headline": "headline",
        "summary": "use Bearer abc.def for api",
        "impact": "",
        "next_action": "",
        "details": ["path C:\\Users\\bob\\secret"],
    }
    cleaned, report = validate_and_repair(candidate, _ctx())
    assert report.sanitizer_applied
    assert "Bearer" not in cleaned["summary"]
    assert "[redacted]" in cleaned["summary"]
    assert "[path]" in cleaned["details"][0]


def test_validate_and_repair_flags_unsupported_number():
    candidate = {
        "headline": "headline 7",
        "summary": "ok", "impact": "", "next_action": "", "details": [],
    }
    ctx = _ctx(source_facts={"hit_count": 3})
    cleaned, report = validate_and_repair(candidate, ctx)
    codes = {v.code for v in report.violations}
    assert "unsupported_number" in codes


def test_validate_and_repair_passes_supported_numbers():
    candidate = {
        "headline": "headline 3",
        "summary": "ok",
        "impact": "", "next_action": "", "details": [],
    }
    ctx = _ctx(source_facts={"hit_count": 3})
    cleaned, report = validate_and_repair(candidate, ctx)
    codes = {v.code for v in report.violations}
    assert "unsupported_number" not in codes


def test_validate_and_repair_route_mismatch_finish_claims_tool():
    candidate = {
        "headline": "ok", "summary": "ok", "impact": "",
        "next_action": "调用 TestPlanGeneratorTool", "details": [],
    }
    _, report = validate_and_repair(candidate, _ctx(action="finish"))
    codes = {v.code for v in report.violations}
    assert "route_mismatch_finish_claims_tool" in codes


def test_validate_and_repair_route_mismatch_ask_user_continues():
    candidate = {
        "headline": "ok", "summary": "自动继续", "impact": "",
        "next_action": "", "details": [],
    }
    _, report = validate_and_repair(candidate, _ctx(action="ask_user"))
    codes = {v.code for v in report.violations}
    assert "route_mismatch_ask_user_continues" in codes


def test_validate_and_repair_scope_overflow():
    candidate = {
        "headline": "ok", "summary": "ok", "impact": "",
        "next_action": "修复 section_id=secret_section", "details": [],
    }
    ctx = _ctx(scope_ids=["allowed_a", "allowed_b"])
    _, report = validate_and_repair(candidate, ctx)
    codes = {v.code for v in report.violations}
    assert "scope_overflow" in codes


def test_validate_and_repair_length_warning_does_not_block():
    candidate = {
        "headline": "h" * 80,
        "summary": "x" * 200,
        "impact": "", "next_action": "", "details": [],
    }
    _, report = validate_and_repair(candidate, _ctx())
    severities = [v.severity for v in report.violations]
    assert "block" not in severities
    assert report.passed is True


def test_validate_and_repair_returns_evaluated_candidate():
    candidate = {
        "headline": "ok", "summary": "ok", "impact": "",
        "next_action": "", "details": [],
    }
    cleaned, _ = validate_and_repair(candidate, _ctx())
    assert "headline" in cleaned


def test_quality_sanitize_replaces_internal_terms_with_placeholder():
    from app.agent_runtime._shared.narrative_governance.quality_validator import (
        sanitize_text as qv_sanitize_text,
    )
    sanitised = qv_sanitize_text("task_internal_id failure")
    assert "task_internal_id" not in sanitised
    assert "[internal-term]" in sanitised
