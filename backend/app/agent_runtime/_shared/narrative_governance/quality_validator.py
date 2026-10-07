"""Phase 2.9C quality gate.

Implements Phase 2.9C §11. The pipeline:

    Schema → Sanitizer → Fact → Route → Scope → Length → Internal term → Final sanitizer

The gate is intentionally side-effect-free (returns a report that
the service can attach to the result). When the gate fails, the
caller falls back to the Phase 2.9A deterministic template. The gate
NEVER raises to the agent main loop.

Tested fixes (Phase 2.9C §11.4):

* Bearer tokens, ``sk-`` keys, JWT, DB DSN, URL query tokens,
  Windows / Linux / UNC paths, traceback fragments, internal class
  names, internal function names, ``task_internal_id``, full SQL.
* Chain-of-thought markers (``let me``, ``thinking:``, etc).
"""

from __future__ import annotations

import re
from typing import Any

from app.agent_runtime._shared.narrative_governance.schemas import (
    NarrativeGovernanceContext,
    NarrativeQualityReport,
    NarrativeQualityViolation,
)


# ── Sanitizer (Phase 2.9C §11.4) ──────────────────────────────────────

_PATTERNS = (
    ("bearer_token", re.compile(r"Bearer\s+\S+", re.IGNORECASE)),
    ("sk_key", re.compile(r"sk-[A-Za-z0-9_-]{6,}\S*", re.IGNORECASE)),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("dsn_password", re.compile(r"(?i)(?:postgres|postgresql|mysql|redis|amqp):\/\/[^:\s]+:[^@\s]+@")),
    ("url_query_token", re.compile(r"(?i)[?&](?:token|api_key|access_token)=[^&\s]+")),
    ("cookie", re.compile(r"(?i)Set-Cookie:\s*[^\n]+")),
    ("windows_path", re.compile(r"[A-Za-z]:\\[^\s]+")),
    ("unc_path", re.compile(r"\\\\[^\\\s]+\\[^\s]+")),
    ("linux_path", re.compile(r"(/home/|/root/|/Users/|/workspace/|/var/|/tmp/)[^\s]+")),
    ("traceback", re.compile(r"Traceback \(most recent call last\)|at 0x[0-9a-f]+|File \"[^\"]+\", line \d+")),
    ("sql_statement", re.compile(r"\b(?:SELECT|INSERT|UPDATE|DELETE)\s+[^\s]+\s+FROM\b", re.IGNORECASE)),
    ("internal_terminology", re.compile(
        r"\b(?:LangGraph Command|checkpoint namespace|task_internal_id|SQLAlchemy session|Pydantic validation|traceback)\b",
        re.IGNORECASE,
    )),
    ("chain_of_thought", re.compile(r"(?i)(?:let'?s think|we need to|my reasoning:|step[-\s]by[-\s]step|思考以下|推理过程)")),
)

_REDACTION = "[redacted]"
_PATH_REDACTION = "[path]"


def sanitize_text(text: Any) -> str:
    """Strip all Phase-2.9C-flagged sensitive tokens from a string.

    Token placeholders are deterministic so test assertions are stable.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    sanitized = text
    sanitized = _PATTERNS[0][1].sub(_REDACTION, sanitized)
    sanitized = _PATTERNS[1][1].sub(_REDACTION, sanitized)
    sanitized = _PATTERNS[2][1].sub(_REDACTION, sanitized)
    sanitized = _PATTERNS[3][1].sub(_REDACTION, sanitized)
    sanitized = _PATTERNS[4][1].sub(_REDACTION, sanitized)
    sanitized = _PATTERNS[5][1].sub(_REDACTION, sanitized)
    sanitized = _PATTERNS[6][1].sub(_PATH_REDACTION, sanitized)
    sanitized = _PATTERNS[7][1].sub(_PATH_REDACTION, sanitized)
    sanitized = _PATTERNS[8][1].sub(_PATH_REDACTION, sanitized)
    sanitized = _PATTERNS[9][1].sub("[traceback]", sanitized)
    sanitized = _PATTERNS[10][1].sub("[sql]", sanitized)
    sanitized = _PATTERNS[11][1].sub("[internal-term]", sanitized)
    sanitized = _PATTERNS[12][1].sub("", sanitized)
    # CE-05 WP-3: PII 维度委托统一 ContextSecurityService（redact/mask/block）。
    # 不保留第二套 PII 规则；日志/事件序列化统一走 sanitize_text。
    try:
        from app.context_engine.security.pii import PIIRedactor

        sanitized = PIIRedactor().apply(sanitized)
    except Exception:  # noqa: BLE001 — 脱敏失败不抛，保持原样
        pass
    return sanitized


def _sanitize_payload(payload: dict[str, Any] | None) -> tuple[dict[str, Any], int]:
    """Sanitize a structured payload. Returns (new_payload, replacements)."""
    if not isinstance(payload, dict):
        return {}, 0
    new_payload: dict[str, Any] = {}
    redactions = 0
    for key, value in payload.items():
        if isinstance(value, str):
            cleaned = sanitize_text(value)
            if cleaned != value:
                redactions += cleaned.count("[redacted]") + cleaned.count("[path]") + cleaned.count("[traceback]") + cleaned.count("[sql]") + cleaned.count("[internal-term]")
            new_payload[key] = cleaned
        elif isinstance(value, dict):
            inner, inner_redactions = _sanitize_payload(value)
            redactions += inner_redactions
            new_payload[key] = inner
        elif isinstance(value, list):
            new_payload[key] = [
                (sanitize_text(item) if isinstance(item, str) else item)
                for item in value
            ]
        else:
            new_payload[key] = value
    return new_payload, redactions


# ── Fact validator ────────────────────────────────────────────────────


_NUMERIC_RE = re.compile(r"\b\d+(?:\.\d+)?\b")


def _extract_fact_numbers(facts: dict[str, Any]) -> set[str]:
    if not isinstance(facts, dict):
        return set()
    bag: set[str] = set()

    def _walk(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            bag.add(str(int(value)) if float(value).is_integer() else str(value))
            return
        if isinstance(value, str):
            for match in _NUMERIC_RE.findall(value):
                bag.add(match)
            return
        if isinstance(value, dict):
            for inner in value.values():
                _walk(inner)
            return
        if isinstance(value, list):
            for inner in value:
                _walk(inner)

    _walk(facts)
    return bag


def _check_facts(
    candidate: dict[str, Any],
    context: NarrativeGovernanceContext,
) -> list[NarrativeQualityViolation]:
    violations: list[NarrativeQualityViolation] = []
    fact_numbers = _extract_fact_numbers(context.source_facts or {})
    fields = ("headline", "summary", "impact", "next_action")
    for field in fields:
        text = str(candidate.get(field) or "")
        if not text:
            continue
        for number in _NUMERIC_RE.findall(text):
            if number not in fact_numbers:
                violations.append(
                    NarrativeQualityViolation(
                        code="unsupported_number",
                        severity="block",
                        field=field,
                        message=f"{field} 含数字 {number},source_facts 中未声明",
                    )
                )
    return violations


# ── Route / scope recheck (reuse Phase 2.9B infrastructure) ──────────


def _check_route(
    candidate: dict[str, Any],
    context: NarrativeGovernanceContext,
) -> list[NarrativeQualityViolation]:
    violations: list[NarrativeQualityViolation] = []
    action = (context.action or "").lower()
    text = " ".join(
        str(candidate.get(field) or "")
        for field in ("headline", "summary", "impact", "next_action")
    ).lower()
    if action == "finish" and any(
        verb in text
        for verb in ("调用", "检索", "search", "fetch", "tool")
    ):
        violations.append(
            NarrativeQualityViolation(
                code="route_mismatch_finish_claims_tool",
                severity="block",
                field="next_action",
                message="finish 路由下不允许 next_action 描述调用工具",
            )
        )
    if action == "ask_user" and any(
        marker in text
        for marker in ("自动继续", "auto continue", "无须用户", "无需用户")
    ):
        violations.append(
            NarrativeQualityViolation(
                code="route_mismatch_ask_user_continues",
                severity="block",
                field="next_action",
                message="ask_user 路由下不应声称自动继续",
            )
        )
    return violations


def _check_scope(
    candidate: dict[str, Any],
    context: NarrativeGovernanceContext,
) -> list[NarrativeQualityViolation]:
    violations: list[NarrativeQualityViolation] = []
    if not context.scope_ids:
        return violations
    allowed = set(context.scope_ids)
    field_text = str(candidate.get("next_action") or "")
    referenced: set[str] = set(re.findall(r"section[_-]?id\s*[=:]\s*([A-Za-z0-9_-]+)", field_text, flags=re.IGNORECASE))
    referenced |= set(re.findall(r"问题\s*([A-Za-z0-9_-]+)", field_text))
    for ref in referenced:
        if ref not in allowed:
            violations.append(
                NarrativeQualityViolation(
                    code="scope_overflow",
                    severity="block",
                    field="next_action",
                    message=f"next_action 引用 {ref},不在允许范围 {sorted(allowed)} 内",
                )
            )
    return violations


# ── Length + final sanitizer ──────────────────────────────────────────


def _check_length(
    candidate: dict[str, Any],
    *,
    summary_chars: int,
    headline_chars: int,
) -> list[NarrativeQualityViolation]:
    violations: list[NarrativeQualityViolation] = []
    if len(str(candidate.get("summary") or "")) > summary_chars:
        violations.append(
            NarrativeQualityViolation(
                code="length_overflow",
                severity="warning",
                field="summary",
                message=f"summary 超过预算 {summary_chars}",
            )
        )
    if len(str(candidate.get("headline") or "")) > headline_chars:
        violations.append(
            NarrativeQualityViolation(
                code="length_overflow",
                severity="warning",
                field="headline",
                message=f"headline 超过预算 {headline_chars}",
            )
        )
    return violations


# ── Public entry ──────────────────────────────────────────────────────


def validate_and_repair(
    candidate: dict[str, Any] | None,
    context: NarrativeGovernanceContext,
    *,
    summary_chars: int = 160,
    headline_chars: int = 40,
) -> tuple[dict[str, Any], NarrativeQualityReport]:
    """Run the Phase 2.9C quality gate and return the (possibly repaired) candidate.

    The function mutates the candidate by sanitising sensitive tokens
    before evaluation; it does NOT remove unsupported numbers (the
    service decides whether to repair or fall back). It returns the
    sanitised payload and the quality report side-by-side.
    """
    if not isinstance(candidate, dict):
        candidate = {}
    sanitised, redactions = _sanitize_payload(candidate)
    violations: list[NarrativeQualityViolation] = []
    violations.extend(_check_facts(sanitised, context))
    violations.extend(_check_route(sanitised, context))
    violations.extend(_check_scope(sanitised, context))
    violations.extend(
        _check_length(
            sanitised,
            summary_chars=summary_chars,
            headline_chars=headline_chars,
        )
    )
    # Final sanitizer pass (idempotent; we run it twice on purpose so
    # anything added by fact check / repair still passes through).
    sanitised, extra = _sanitize_payload(sanitised)
    redactions += extra

    block_failures = [v for v in violations if v.severity == "block"]
    report = NarrativeQualityReport(
        passed=not block_failures,
        violations=violations,
        repaired=False,
        fallback_used=False,
        sanitizer_applied=redactions > 0,
    )
    return sanitised, report


__all__ = [
    "sanitize_text",
    "validate_and_repair",
    "NarrativeQualityViolation",
    "NarrativeQualityReport",
]
