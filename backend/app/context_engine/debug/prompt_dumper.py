"""Context Engine — dev-only prompt 全量落盘（.md + index.jsonl）。

CONTEXT_PROMPT_DUMP_ENABLED=1 时,把每次 ContextEngine.compose() 装配出的完整
prompt 写到本地 markdown 文件,头部标注 call_site / profile / purpose(来自
Profile.description,中文说明),正文是 verbatim prompt。生产默认关闭,关闭时
零行为零开销。

设计原则:
- 不动 ContextSnapshot 表、不动 audit API
- 走 DI(可选注入),不强制每个调用方构造
- 失败永不抛 — try/except + logger.warning
- 复用 app.common.prompt_dump._sanitize() 做 API key 脱敏
- 复用 security.safe_excerpt.redact_user() 做 user_id 脱敏
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.common.prompt_dump import _sanitize as _sanitize_secrets
from app.context_engine.models.compose import ContextComposeResult
from app.context_engine.models.context import ContextPlan, ContextRequest
from app.context_engine.profiles.registry import (
    CALL_SITE_TO_PROFILE_KEY,
    get_default_profile_registry,
)

logger = logging.getLogger(__name__)

_ENV_ENABLED = "CONTEXT_PROMPT_DUMP_ENABLED"
_ENV_DUMP_DIR = "CONTEXT_PROMPT_DUMP_DIR"
_DEFAULT_DUMP_DIR = "./tmp/prompt_dumps"
_PRODUCTION_APP_ENVS = frozenset({"production", "prod"})

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _dump_dir_from_env() -> Path:
    """读取 dump 目录 env(默认 ./tmp/prompt_dumps)。不强制创建。"""
    raw = os.getenv(_ENV_DUMP_DIR, "").strip()
    return Path(raw).resolve() if raw else Path(_DEFAULT_DUMP_DIR).resolve()


@dataclass(slots=True)
class PromptDumpRecord:
    """单次 dump 的索引条目,写入 index.jsonl。"""

    snapshot_public_id: str
    call_site: str
    purpose: str
    profile_key: str
    profile_version: str
    execution_mode: str
    estimated_input_tokens: int
    input_budget: int
    included_count: int
    dropped_count: int
    file: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_public_id": self.snapshot_public_id,
            "call_site": self.call_site,
            "purpose": self.purpose,
            "profile_key": self.profile_key,
            "profile_version": self.profile_version,
            "execution_mode": self.execution_mode,
            "estimated_input_tokens": self.estimated_input_tokens,
            "input_budget": self.input_budget,
            "included_count": self.included_count,
            "dropped_count": self.dropped_count,
            "file": self.file,
            "created_at": self.created_at,
        }


class ContextPromptDumper:
    """开发态 prompt 落盘器。enabled=False 时所有方法零行为。"""

    def __init__(
        self,
        *,
        enabled: bool = False,
        dump_dir: str | Path | None = None,
        max_prompt_chars: int = 200_000,
    ) -> None:
        requested = bool(enabled)
        app_env = os.getenv("APP_ENV", "development").strip().lower()
        # Prompt dumps contain user content even after secret sanitisation.
        # A deployment profile must therefore never activate them merely by
        # carrying a stale debug flag from development.
        self._enabled = requested and app_env not in _PRODUCTION_APP_ENVS
        self._dump_dir = Path(dump_dir).resolve() if dump_dir else _dump_dir_from_env()
        self._max_prompt_chars = int(max_prompt_chars)
        self._session_id: str = uuid.uuid4().hex[:12]
        self._session_started: str = datetime.now(timezone.utc).isoformat()
        self._registry = get_default_profile_registry()
        if requested and not self._enabled:
            logger.error(
                "ContextPromptDumper blocked in production-like APP_ENV=%s",
                app_env or "production",
            )
        elif self._enabled:
            logger.warning(
                "ContextPromptDumper ENABLED (dev-only): prompts will be written to %s. "
                "Do NOT enable in production.",
                self._dump_dir,
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def dump_dir(self) -> Path:
        return self._dump_dir

    def dump(
        self,
        *,
        request: ContextRequest,
        plan: ContextPlan,
        composed: ContextComposeResult,
        snapshot_public_id: str,
        execution_mode: str = "active",
    ) -> str | None:
        """写一个 .md + append 一行到 index.jsonl。返回相对路径或 None。"""
        if not self._enabled:
            return None
        try:
            ts = datetime.now(timezone.utc)
            call_site = (request.call_site or "unknown").strip()
            purpose = self._resolve_purpose(plan, call_site)
            target_path = self._target_path(ts, call_site, snapshot_public_id)
            rel_path = self._rel(target_path)
            markdown = self._render_markdown(
                request=request,
                plan=plan,
                composed=composed,
                snapshot_public_id=snapshot_public_id,
                execution_mode=execution_mode,
                purpose=purpose,
                ts=ts,
            )
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(markdown, encoding="utf-8")
            record = PromptDumpRecord(
                snapshot_public_id=snapshot_public_id,
                call_site=call_site,
                purpose=purpose,
                profile_key=str(plan.profile_key),
                profile_version=str(plan.profile_version),
                execution_mode=execution_mode,
                estimated_input_tokens=int(composed.estimated_input_tokens),
                input_budget=int(plan.input_budget or 0),
                included_count=len(composed.selected.included) if composed.selected else 0,
                dropped_count=len(composed.selected.dropped) if composed.selected else 0,
                file=rel_path,
                created_at=ts.isoformat(),
            )
            self._append_index(record)
            return rel_path
        except Exception as exc:  # noqa: BLE001
            logger.warning("ContextPromptDumper.dump failed: %s", exc)
            return None

    # ── 内部 ────────────────────────────────────────────────────────

    def _resolve_purpose(self, plan: ContextPlan, call_site: str) -> str:
        """purpose = Profile.description;回退 '未登记 call_site'。永不抛。"""
        try:
            for key in (plan.profile_key, CALL_SITE_TO_PROFILE_KEY.get(call_site)):
                if not key:
                    continue
                profile = self._registry.get_or_none(key)
                if profile and profile.description:
                    return profile.description
        except Exception:  # noqa: BLE001
            pass
        return f"（未登记 call_site：{call_site or '?'}，无 Profile 描述）"

    def _target_path(self, ts: datetime, call_site: str, snapshot_public_id: str) -> Path:
        slug_call = _SAFE_NAME_RE.sub("_", call_site)[:48] or "unknown"
        slug_snap = _SAFE_NAME_RE.sub("_", snapshot_public_id)[:48] or "snap"
        stamp = ts.strftime("%H%M%S") + f"_{ts.microsecond // 1000:03d}"
        date_dir = ts.strftime("%Y-%m-%d")
        filename = f"{stamp}_{slug_call}_{slug_snap}.md"
        return self._dump_dir / date_dir / filename

    def _rel(self, abs_path: Path) -> str:
        try:
            return str(abs_path.relative_to(self._dump_dir))
        except ValueError:
            return str(abs_path)

    def _render_markdown(
        self,
        *,
        request: ContextRequest,
        plan: ContextPlan,
        composed: ContextComposeResult,
        snapshot_public_id: str,
        execution_mode: str,
        purpose: str,
        ts: datetime,
    ) -> str:
        from app.context_engine.security.safe_excerpt import redact_user

        user_redacted = redact_user(str(getattr(request, "user_id", "") or ""))
        fence = self._choose_fence(composed.prompt_text or "")

        budget_rows = [
            ("context_window", plan.model_context_window),
            ("input_budget", plan.input_budget),
            ("output_reserve", plan.output_reserve),
            ("runtime_reserve", plan.runtime_reserve),
            ("safety_margin", plan.safety_margin),
            ("estimated_input", composed.estimated_input_tokens),
        ]
        budget_lines = "\n".join(
            f"| {name} | {self._fmt_num(val)} |" for name, val in budget_rows
        )

        sections_md = self._render_sections(composed)
        included_md = self._render_included(composed)
        dropped_md = self._render_dropped(composed)
        messages_md = self._render_messages(composed)
        prompt_block = _sanitize_prompt(self._truncate(composed.prompt_text or ""))

        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S UTC")

        return (
            f"# Prompt Dump — {request.call_site or 'unknown'}\n\n"
            f"> **用途**: {purpose}\n\n"
            f"## Metadata\n\n"
            f"- snapshot_public_id: `{snapshot_public_id}`\n"
            f"- call_site: `{request.call_site or '?'}`\n"
            f"- llm_task_type: `{self._llm_task_type(request)}`\n"
            f"- context_profile: `{plan.profile_key}` ({plan.profile_version})\n"
            f"- execution_mode: `{execution_mode}`\n"
            f"- user_id: `{user_redacted}` (redacted)\n"
            f"- conversation_id: {getattr(request, 'conversation_id', None) or '?'}\n"
            f"- agent_task_id: {getattr(request, 'task_id', None) or '?'}\n"
            f"- timestamp: `{ts_str}`\n\n"
            f"## Budget\n\n"
            f"| field | tokens |\n|---|---|\n{budget_lines}\n\n"
            f"## Sections\n\n"
            f"{sections_md}\n\n"
            f"## Included sources\n\n"
            f"{included_md}\n\n"
            f"## Dropped sources\n\n"
            f"{dropped_md}\n\n"
            f"## Messages\n\n"
            f"{messages_md}\n\n"
            f"## Full prompt (verbatim)\n\n"
            f"{fence}text\n{prompt_block}\n{fence}\n"
        )

    @staticmethod
    def _llm_task_type(request: ContextRequest) -> str:
        cs = (request.call_site or "context").split(".")[-1][:64]
        return cs or "context"

    @staticmethod
    def _fmt_num(val: Any) -> str:
        if val is None:
            return "—"
        return f"{int(val):,}"

    @staticmethod
    def _choose_fence(body: str) -> str:
        """根据正文里最长连续反引号选 fence,至少 4 个。"""
        longest = 0
        run = 0
        for ch in body:
            if ch == "`":
                run += 1
                if run > longest:
                    longest = run
            else:
                run = 0
        return "`" * max(4, longest + 1)

    @staticmethod
    def _truncate(text: str) -> str:
        if len(text) <= 200_000:
            return text
        return text[:200_000] + "\n\n[TRUNCATED — prompt exceeds 200,000 chars]\n"

    @staticmethod
    def _render_sections(composed: ContextComposeResult) -> str:
        stats = composed.section_stats or {}
        if not stats:
            return "（无 section_stats 数据）\n"
        lines = [
            "| section_id | required | included | dropped | budget | used |",
            "|---|---|---|---|---|---|",
        ]
        for sid in sorted(stats.keys()):
            s = stats[sid] or {}
            lines.append(
                f"| `{sid}` | "
                f"{s.get('required', '—')} | "
                f"{s.get('included_count', '—')} | "
                f"{s.get('dropped_count', '—')} | "
                f"{ContextPromptDumper._fmt_num(s.get('budget_tokens'))} | "
                f"{ContextPromptDumper._fmt_num(s.get('estimated_tokens'))} |"
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_included(composed: ContextComposeResult) -> str:
        if not composed.selected or not composed.selected.included:
            return "（无 included 源）\n"
        lines = []
        for item in composed.selected.included:
            kind = getattr(item.kind, "value", str(item.kind))
            tokens = getattr(item, "estimated_tokens", None)
            tokens_str = ContextPromptDumper._fmt_num(tokens)
            ref = item.source_ref or item.item_id
            lines.append(f"- `{kind}/{item.source_type or '?'}` ref=`{ref}` — {tokens_str} tokens")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_dropped(composed: ContextComposeResult) -> str:
        if not composed.selected or not composed.selected.dropped:
            return "（无 dropped 源）\n"
        lines = []
        for d in composed.selected.dropped:
            detail = f" — {d.detail}" if d.detail else ""
            lines.append(f"- `{d.item_id}` — reason: `{d.reason}`{detail}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_messages(composed: ContextComposeResult) -> str:
        if not composed.messages:
            return "（无结构化消息）\n"
        lines = []
        for idx, m in enumerate(composed.messages, 1):
            kind = getattr(m.kind, "value", "?")
            trust = getattr(m.trust, "value", str(m.trust))
            locked = " [LOCKED]" if m.locked else ""
            lines.append(
                f"{idx}. [{m.role}] kind=`{kind}` trust=`{trust}`{locked} "
                f"source=`{m.source_ref or '?'}` section=`{m.section_id or '?'}`"
            )
        return "\n".join(lines) + "\n"

    def _append_index(self, record: PromptDumpRecord) -> None:
        index_path = self._dump_dir / "index.jsonl"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        header = json.dumps(
            {
                "session_id": self._session_id,
                "started_at": self._session_started,
                "event": "session_started",
            },
            ensure_ascii=False,
        ) + "\n"
        if not index_path.exists():
            index_path.write_text(header, encoding="utf-8")
        with index_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")


# 防止循环引用,运行时再 sanitize prompt 文本
def _sanitize_prompt(prompt_text: str) -> str:
    """复用 app.common.prompt_dump 的 key 脱敏规则。"""
    return _sanitize_secrets(prompt_text)
# auto-appended module-level note: prompt dumper: dev-only 把完整 prompt 落盘(供排查)。
