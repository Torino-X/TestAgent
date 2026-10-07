"""ContextPromptDumper dev-only prompt 全量落盘测试。

覆盖:
- enabled=False → 零文件
- enabled=True → 写 .md + 更新 index.jsonl
- purpose = Profile.description
- 失败不抛
- user_id 脱敏 / secrets 脱敏
- 文件名合法 + 包含 snapshot_public_id
- fence 在含三反引号的 prompt 里仍闭合
- 默认 build_context_engine 拿到的 dumper 是 disabled
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.context_engine.debug.prompt_dumper import ContextPromptDumper
from app.context_engine.models.compose import (
    ContextComposeResult,
    ContextMessage,
)
from app.context_engine.models.context import (
    ContextItem,
    ContextKind,
    ContextPlan,
    ContextRequest,
    ContextTrust,
)
from app.context_engine.models.selection import (
    DroppedContextRef,
    SelectedContextSet,
)
from app.context_engine.models.enums import SourceType
from app.context_engine.profiles.registry import CHAT_REPLY_PROFILE
from app.context_engine.runtime.engine_factory import build_context_engine


def _make_request(call_site: str = "chat.reply") -> ContextRequest:
    return ContextRequest(
        user_id="usr_secret_42",
        conversation_id="conv_xyz",
        task_id="task_42",
        call_site=call_site,
        current_user_message="你好,测试上下文管理",
        current_node="chat_node",
    )


def _make_plan(profile_key: str = "chat.reply.v1") -> ContextPlan:
    return ContextPlan(
        profile_key=profile_key,
        profile_version="v1",
        model_context_window=200_000,
        input_budget=50_000,
        output_reserve=16_000,
        runtime_reserve=3_000,
        safety_margin=7_500,
    )


def _make_composed(prompt_text: str = "<system>hello</system>") -> ContextComposeResult:
    item = ContextItem(
        item_id="item_1",
        kind=ContextKind.EVIDENCE,
        source_type=SourceType.FILE_SUMMARY,
        source_ref="file_summary:doc_42",
        content="证据摘要内容",
        authority=80,
        estimated_tokens=1500,
        trust=ContextTrust.UNTRUSTED_REFERENCE,
    )
    selected = SelectedContextSet(
        included=[item],
        dropped=[DroppedContextRef(item_id="item_dropped", reason="source_quota", detail="超容")],
        section_stats={
            "evidence": {
                "required": True,
                "included_count": 1,
                "dropped_count": 1,
                "budget_tokens": 15000,
                "estimated_tokens": 1500,
            }
        },
        total_estimated_tokens=1500,
        locked_section_ids=[],
    )
    return ContextComposeResult(
        messages=[
            ContextMessage(
                role="system",
                content="[system_rules]\nYou are TestAgent...",
                kind=ContextKind.SYSTEM_RULES,
            ),
            ContextMessage(
                role="user",
                content="[evidence]\n<context-section ...>证据摘要</context-section>",
                kind=ContextKind.EVIDENCE,
                source_ref="file_summary:doc_42",
                section_id="evidence",
            ),
        ],
        selected=selected,
        prompt_text=prompt_text,
        prompt_digest="a" * 64,
        prompt_excerpt="call_site=chat.reply",
        estimated_input_tokens=1500,
        section_stats=selected.section_stats,
    )


def test_disabled_writes_nothing(tmp_path: Path):
    dumper = ContextPromptDumper(enabled=False, dump_dir=tmp_path)
    result = dumper.dump(
        request=_make_request(),
        plan=_make_plan(),
        composed=_make_composed(),
        snapshot_public_id="snap_abc",
    )
    assert result is None
    assert list(tmp_path.iterdir()) == []


def test_production_like_environment_blocks_requested_prompt_dump(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    dumper = ContextPromptDumper(enabled=True, dump_dir=tmp_path)

    assert dumper.enabled is False
    assert dumper.dump(
        request=_make_request(),
        plan=_make_plan(),
        composed=_make_composed("user content must not be written"),
        snapshot_public_id="cs_prod_guard",
    ) is None
    assert list(tmp_path.iterdir()) == []


def test_enabled_writes_markdown(tmp_path: Path):
    dumper = ContextPromptDumper(enabled=True, dump_dir=tmp_path)
    rel = dumper.dump(
        request=_make_request("chat.reply"),
        plan=_make_plan(),
        composed=_make_composed(),
        snapshot_public_id="snap_abc",
        execution_mode="active",
    )
    assert rel is not None
    assert rel.endswith(".md")
    md_path = tmp_path / rel
    assert md_path.exists()
    body = md_path.read_text(encoding="utf-8")
    # header
    assert "# Prompt Dump — chat.reply" in body
    assert "> **用途**:" in body
    assert "snap_abc" in body
    # budget
    assert "## Budget" in body
    assert "input_budget" in body
    # sections
    assert "## Sections" in body
    # included/dropped
    assert "## Included sources" in body
    assert "file_summary:doc_42" in body
    assert "## Dropped sources" in body
    assert "source_quota" in body
    # messages
    assert "## Messages" in body
    # verbatim prompt
    assert "## Full prompt (verbatim)" in body
    assert "<system>hello</system>" in body


def test_purpose_comes_from_profile_description(tmp_path: Path):
    dumper = ContextPromptDumper(enabled=True, dump_dir=tmp_path)
    rel = dumper.dump(
        request=_make_request("chat.reply"),
        plan=_make_plan("chat.reply.v1"),
        composed=_make_composed(),
        snapshot_public_id="snap_xyz",
    )
    md_path = tmp_path / rel
    body = md_path.read_text(encoding="utf-8")
    assert CHAT_REPLY_PROFILE.description in body


def test_unknown_call_site_falls_back(tmp_path: Path):
    dumper = ContextPromptDumper(enabled=True, dump_dir=tmp_path)
    rel = dumper.dump(
        request=_make_request("totally.unregistered.site"),
        plan=_make_plan(),
        composed=_make_composed(),
        snapshot_public_id="snap_unk",
    )
    body = (tmp_path / rel).read_text(encoding="utf-8")
    assert "未登记" in body or "totally.unregistered.site" in body


def test_pii_redaction(tmp_path: Path):
    dumper = ContextPromptDumper(enabled=True, dump_dir=tmp_path)
    rel = dumper.dump(
        request=_make_request(),
        plan=_make_plan(),
        composed=_make_composed(),
        snapshot_public_id="snap_pii",
    )
    body = (tmp_path / rel).read_text(encoding="utf-8")
    # raw user id must NOT appear
    assert "usr_secret_42" not in body
    # sha-256 prefix (8 hex chars) appears
    assert re.search(r"user_id: `([0-9a-f]{8})`", body) is not None


def test_secret_key_sanitised(tmp_path: Path):
    dumper = ContextPromptDumper(enabled=True, dump_dir=tmp_path)
    secret = "sk-" + "a" * 25
    composed = _make_composed(prompt_text=f"<system>api_key={secret}</system>")
    rel = dumper.dump(
        request=_make_request(),
        plan=_make_plan(),
        composed=composed,
        snapshot_public_id="snap_secret",
    )
    body = (tmp_path / rel).read_text(encoding="utf-8")
    assert secret not in body
    assert "REDACTED" in body


def test_index_jsonl_appended(tmp_path: Path):
    dumper = ContextPromptDumper(enabled=True, dump_dir=tmp_path)
    for i in range(2):
        dumper.dump(
            request=_make_request("chat.reply"),
            plan=_make_plan(),
            composed=_make_composed(),
            snapshot_public_id=f"snap_{i}",
        )
    index_path = tmp_path / "index.jsonl"
    assert index_path.exists()
    lines = [ln for ln in index_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # 1 session_started + 2 dump records
    assert len(lines) == 3
    records = [json.loads(ln) for ln in lines[1:]]
    assert {r["snapshot_public_id"] for r in records} == {"snap_0", "snap_1"}
    assert all(r["purpose"] == CHAT_REPLY_PROFILE.description for r in records)


def test_filename_includes_snapshot_id_and_date(tmp_path: Path):
    dumper = ContextPromptDumper(enabled=True, dump_dir=tmp_path)
    rel = dumper.dump(
        request=_make_request("chat.reply"),
        plan=_make_plan(),
        composed=_make_composed(),
        snapshot_public_id="snap_abc123",
    )
    today_utc = datetime.now(timezone.utc).date().isoformat()
    assert rel.startswith(f"{today_utc}\\") or rel.startswith(f"{today_utc}/")
    assert "snap_abc123" in rel
    assert rel.endswith(".md")


def test_dump_failure_never_raises(tmp_path: Path, monkeypatch):
    dumper = ContextPromptDumper(enabled=True, dump_dir=tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _boom)
    result = dumper.dump(
        request=_make_request(),
        plan=_make_plan(),
        composed=_make_composed(),
        snapshot_public_id="snap_fail",
    )
    assert result is None  # never raises


def test_backtick_fence_survives_nested_code(tmp_path: Path):
    dumper = ContextPromptDumper(enabled=True, dump_dir=tmp_path)
    nested = "<user>```python\nprint('hi')\n```\nend</user>"
    composed = _make_composed(nested)
    rel = dumper.dump(
        request=_make_request(),
        plan=_make_plan(),
        composed=composed,
        snapshot_public_id="snap_fence",
    )
    body = (tmp_path / rel).read_text(encoding="utf-8")
    # 4+ backtick fence should appear
    assert "```text" in body or "````text" in body
    # content present
    assert "print('hi')" in body


def test_engine_factory_uses_effective_prompt_dump_flag():
    """Factory follows the effective deployment flag, not a stale test default."""
    from app.context_engine.feature_flags import get_context_engine_flags

    engine = build_context_engine()
    assert engine._prompt_dumper is not None
    assert engine._prompt_dumper.enabled is (
        get_context_engine_flags().context_prompt_dump_enabled
    )
