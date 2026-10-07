"""F025 — Tests for the review_standard rule engine in ResultReviewTool."""

from __future__ import annotations

import pytest

from app.tools.result_review_tool import ResultReviewTool


def _eval(rules, sections):
    """Helper: run the rule engine with given rules/sections, return issues."""
    standard = {"version": 1, "rules": rules, "pass_policy": "any_block_fails"}
    return ResultReviewTool()._evaluate_rules(sections, standard)


# ── forbidden_pattern ────────────────────────────────────────────────


def test_forbidden_pattern_block_when_matches():
    rules = [{
        "id": "no_md",
        "kind": "forbidden_pattern",
        "patterns": ["```", "\\*\\*"],
        "severity": "block",
        "applies_to_sections": ["*"],
    }]
    sections = [
        {"section_id": "s1", "title": "A", "content": "这是正文。\n```python\nfoo\n```\n"},
        {"section_id": "s2", "title": "B", "content": "纯文本，无违规"},
    ]
    issues = _eval(rules, sections)
    assert len(issues) == 1
    assert issues[0]["section_id"] == "s1"
    assert issues[0]["severity"] == "block"
    assert issues[0]["kind"] == "forbidden_pattern"


def test_forbidden_pattern_warn_when_matches():
    rules = [{
        "id": "no_md",
        "kind": "forbidden_pattern",
        "patterns": ["```"],
        "severity": "warn",
        "applies_to_sections": ["*"],
    }]
    sections = [{"section_id": "s1", "title": "A", "content": "```bad```"}]
    issues = _eval(rules, sections)
    assert len(issues) == 1
    assert issues[0]["severity"] == "warn"


def test_forbidden_pattern_scope_filters_sections():
    rules = [{
        "id": "no_md",
        "kind": "forbidden_pattern",
        "patterns": ["```"],
        "severity": "block",
        "applies_to_sections": ["s2"],
    }]
    sections = [
        {"section_id": "s1", "title": "A", "content": "```bad```"},
        {"section_id": "s2", "title": "B", "content": "```also bad```"},
    ]
    issues = _eval(rules, sections)
    assert len(issues) == 1
    assert issues[0]["section_id"] == "s2"


def test_forbidden_pattern_evidence_and_span():
    rules = [{
        "id": "no_md",
        "kind": "forbidden_pattern",
        "patterns": ["\\*\\*"],
        "severity": "block",
        "applies_to_sections": ["*"],
    }]
    body = "这是 **加粗** 文字"
    sections = [{"section_id": "s1", "title": "A", "content": body}]
    issues = _eval(rules, sections)
    assert len(issues) == 1
    assert "**" in issues[0]["evidence"]
    span = issues[0]["span"]
    assert span is not None
    start, end = span
    assert body[start:end] == "**"


# ── json_key_rename ──────────────────────────────────────────────────


def test_json_key_rename_flags_alt_name():
    rules = [{
        "id": "keys_preserved",
        "kind": "json_key_rename",
        "forbidden_renames": {"trace_id": ["traceId", "traceID"]},
        "severity": "block",
        "applies_to_sections": ["*"],
    }]
    sections = [
        {"section_id": "s1", "content": "本字段的 traceId 应该叫 trace_id"},
    ]
    issues = _eval(rules, sections)
    assert len(issues) == 1
    assert issues[0]["section_id"] == "s1"
    assert "trace_id" in issues[0]["message"]


def test_json_key_rename_passes_when_correct():
    rules = [{
        "id": "keys_preserved",
        "kind": "json_key_rename",
        "forbidden_renames": {"trace_id": ["traceId"]},
        "severity": "block",
        "applies_to_sections": ["*"],
    }]
    sections = [{"section_id": "s1", "content": "字段名是 trace_id 没有问题"}]
    issues = _eval(rules, sections)
    assert issues == []


# ── table_header_whitelist ───────────────────────────────────────────


def test_table_header_whitelist_flags_extra_keys():
    rules = [{
        "id": "exec_results_header",
        "kind": "table_header_whitelist",
        "section_id": "s_exec",
        "whitelist": ["id", "name", "status"],
        "severity": "block",
    }]
    sections = [{
        "section_id": "s_exec",
        "content": "",
        "tables": [
            [
                {"id": 1, "name": "x", "status": "ok", "extra_col": "bad"},
                {"id": 2, "name": "y", "status": "ok", "extra_col": "bad"},
            ],
        ],
    }]
    issues = _eval(rules, sections)
    assert len(issues) == 1
    msg = issues[0]["message"]
    assert "多余键" in msg or "extra_col" in msg


def test_table_header_whitelist_flags_missing_keys():
    rules = [{
        "id": "exec_results_header",
        "kind": "table_header_whitelist",
        "section_id": "s_exec",
        "whitelist": ["id", "name", "status", "duration"],
        "severity": "block",
    }]
    sections = [{
        "section_id": "s_exec",
        "content": "",
        "tables": [
            [{"id": 1, "name": "x", "status": "ok"}],
        ],
    }]
    issues = _eval(rules, sections)
    assert len(issues) == 1
    assert "duration" in issues[0]["message"]


def test_table_header_whitelist_passes_when_match():
    rules = [{
        "id": "exec_results_header",
        "kind": "table_header_whitelist",
        "section_id": "s_exec",
        "whitelist": ["id", "name"],
        "severity": "block",
    }]
    sections = [{
        "section_id": "s_exec",
        "tables": [[{"id": 1, "name": "x"}, {"id": 2, "name": "y"}]],
    }]
    assert _eval(rules, sections) == []


# ── section_format ──────────────────────────────────────────────────


def test_section_format_bullet_list_passes():
    rules = [{
        "id": "summary_bullets",
        "kind": "section_format",
        "section_id": "s_summary",
        "format": "bullet_list",
        "min_items": 3,
        "severity": "block",
    }]
    sections = [{
        "section_id": "s_summary",
        "content": "- 第一条\n- 第二条\n- 第三条",
    }]
    assert _eval(rules, sections) == []


def test_section_format_bullet_list_fails_when_too_few():
    rules = [{
        "id": "summary_bullets",
        "kind": "section_format",
        "section_id": "s_summary",
        "format": "bullet_list",
        "min_items": 3,
        "severity": "block",
    }]
    sections = [{
        "section_id": "s_summary",
        "content": "只有一条 - bullet",
    }]
    issues = _eval(rules, sections)
    assert len(issues) == 1
    assert "3" in issues[0]["message"]


# ── max_chars ───────────────────────────────────────────────────────


def test_max_chars_warn_when_exceeded():
    rules = [{
        "id": "section_too_long",
        "kind": "max_chars",
        "max_chars": 50,
        "severity": "warn",
    }]
    sections = [{"section_id": "s1", "content": "x" * 100}]
    issues = _eval(rules, sections)
    assert len(issues) == 1
    assert issues[0]["severity"] == "warn"
    assert "100" in issues[0]["evidence"]


def test_max_chars_no_issue_when_under_limit():
    rules = [{
        "id": "section_too_long",
        "kind": "max_chars",
        "max_chars": 100,
        "severity": "warn",
    }]
    sections = [{"section_id": "s1", "content": "short"}]
    assert _eval(rules, sections) == []


# ── dispatcher / integration ────────────────────────────────────────


def test_dispatcher_runs_all_rules_and_accumulates():
    rules = [
        {
            "id": "no_md",
            "kind": "forbidden_pattern",
            "patterns": ["```"],
            "severity": "block",
            "applies_to_sections": ["*"],
        },
        {
            "id": "too_long",
            "kind": "max_chars",
            "max_chars": 10,
            "severity": "warn",
        },
    ]
    sections = [
        {"section_id": "s1", "content": "```\ncode block\n```" + ("x" * 20)},
    ]
    issues = _eval(rules, sections)
    # Both rules fire on s1
    assert len(issues) == 2
    assert {i["severity"] for i in issues} == {"block", "warn"}


def test_dispatcher_handles_empty_standard():
    sections = [{"section_id": "s1", "content": "anything"}]
    assert _eval([], sections) == []
    assert _eval(None, sections) == []


def test_dispatcher_skips_unknown_kind():
    rules = [{"id": "x", "kind": "unknown_kind", "severity": "block"}]
    sections = [{"section_id": "s1", "content": "anything"}]
    assert _eval(rules, sections) == []


def test_dispatcher_rule_exception_does_not_break_others():
    """If one rule raises, others still run (defensive dispatch)."""
    class _Boom:
        kind = "doesnt_matter"

    rules = [
        _Boom(),  # not a dict — dispatcher should skip
        {
            "id": "no_md",
            "kind": "forbidden_pattern",
            "patterns": ["```"],
            "severity": "block",
            "applies_to_sections": ["*"],
        },
    ]
    sections = [{"section_id": "s1", "content": "```bad```"}]
    issues = _eval(rules, sections)
    assert len(issues) == 1
    assert issues[0]["rule_id"] == "no_md"


# ── missing_section (Phase 2.9A.X) ───────────────────────────────────


def test_missing_section_flags_absent_sections():
    """宽容解析/截断场景下，generated_sections 缺少模板期望的章节 → block issue。"""
    rules = [{
        "id": "missing_section",
        "kind": "missing_section",
        "expected_section_ids": ["section_a", "section_b", "section_c"],
        "severity": "block",
        "fix_instruction": "请补生成缺失章节 section_a、section_b、section_c",
    }]
    # 只生成了 section_b，section_a 和 section_c 缺失
    sections = [{"section_id": "section_b", "title": "B", "content": "..."}]
    issues = _eval(rules, sections)
    assert len(issues) == 1
    assert issues[0]["kind"] == "missing_section"
    assert issues[0]["severity"] == "block"
    # fix_instruction 透传
    assert issues[0]["fix_instruction"] == "请补生成缺失章节 section_a、section_b、section_c"
    # missing_section_ids 列表
    assert set(issues[0]["missing_section_ids"]) == {"section_a", "section_c"}
    # message 列出缺失章节
    assert "section_a" in issues[0]["message"]
    assert "section_c" in issues[0]["message"]
    assert "section_b" not in issues[0]["message"]


def test_missing_section_passes_when_all_present():
    """所有期望章节都在 generated_sections 中 → 不 emit issue。"""
    rules = [{
        "id": "missing_section",
        "kind": "missing_section",
        "expected_section_ids": ["section_a", "section_b"],
        "severity": "block",
    }]
    sections = [
        {"section_id": "section_a", "content": "..."},
        {"section_id": "section_b", "content": "..."},
    ]
    assert _eval(rules, sections) == []


def test_missing_section_skips_when_expected_empty():
    """expected_section_ids 为空 → 不 emit issue（防止误判整个测试方案）。"""
    rules = [{
        "id": "missing_section",
        "kind": "missing_section",
        "expected_section_ids": [],
        "severity": "block",
    }]
    sections = [{"section_id": "section_a", "content": "..."}]
    assert _eval(rules, sections) == []


# ── fix_instruction 透传（任意 rule kind） ──────────────────────────


def test_fix_instruction_passthrough_for_table_header_whitelist():
    """table_header_whitelist 规则带 fix_instruction 时，透传到 issue。"""
    rules = [{
        "id": "table_header_section_a",
        "kind": "table_header_whitelist",
        "section_id": "section_a",
        "whitelist": ["作者", "版本号"],
        "severity": "block",
        "fix_instruction": "将 author 改为 作者。允许的键名集合：作者、版本号。",
    }]
    sections = [{
        "section_id": "section_a",
        "tables": [[{"author": "x", "version": "v1"}]],
    }]
    issues = _eval(rules, sections)
    assert len(issues) == 1
    assert "fix_instruction" in issues[0]
    assert issues[0]["fix_instruction"] == "将 author 改为 作者。允许的键名集合：作者、版本号。"
