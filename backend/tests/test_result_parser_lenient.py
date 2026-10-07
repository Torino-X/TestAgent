"""Phase 2.9A.X — Test ``parse_json_lenient`` 与 ``ResultSchemaMismatch`` 行为。"""

from __future__ import annotations

import json

import pytest

from app.common.result_parser import (
    ResultParseError,
    ResultParser,
    ResultSchemaMismatch,
)


# ── parse_json_lenient ────────────────────────────────────────────────


def test_parse_json_lenient_normal_json():
    parser = ResultParser()
    raw = json.dumps({"section_a": {"x": 1}, "section_b": {"y": 2}}, ensure_ascii=False)
    parsed, issues = parser.parse_json_lenient(raw)
    assert parsed is not None
    assert set(parsed.keys()) == {"section_a", "section_b"}
    assert issues == []


def test_parse_json_lenient_with_config_emits_no_issues_when_complete():
    parser = ResultParser()
    config = {"ai_fields": [{"field": "section_a"}, {"field": "section_b"}]}
    raw = json.dumps({"section_a": {}, "section_b": {}}, ensure_ascii=False)
    parsed, issues = parser.parse_json_lenient(raw, config)
    assert parsed is not None
    assert issues == []


def test_parse_json_lenient_partial_recovery_returns_missing_fields():
    parser = ResultParser()
    config = {"ai_fields": [
        {"field": "section_a"}, {"field": "section_b"}, {"field": "section_c"},
    ]}
    # section_a 闭合、section_b 闭合、section_c 未闭合 → slice 到 section_b 闭合
    raw = (
        '{"section_a": {"x": 1}, '
        '"section_b": {"y": 2}, '
        '"section_c": {"z":'
    )
    parsed, issues = parser.parse_json_lenient(raw, config)
    # section_c 不在 slice 范围内，预期 parsed 有 a/b，issues 报 c 缺失
    if parsed is not None:
        # 若 slice 成功，parsed 应该含 a 和 b，c 缺失
        assert "section_a" in parsed
        assert "section_b" in parsed
    fields = [iss["field"] for iss in issues]
    assert "section_c" in fields
    assert all(iss["kind"] == "missing_field" for iss in issues)
    assert all(iss["severity"] == "block" for iss in issues)


def test_parse_json_lenient_severe_truncation_returns_all_missing():
    parser = ResultParser()
    config = {"ai_fields": [
        {"field": "section_a"}, {"field": "section_b"},
    ]}
    # severe 截断：outer 从未闭合 → parsed = None，所有字段 missing
    raw = '{"section_a": {"x":'
    parsed, issues = parser.parse_json_lenient(raw, config)
    assert parsed is None
    assert len(issues) == 2
    fields = {iss["field"] for iss in issues}
    assert fields == {"section_a", "section_b"}


def test_parse_json_lenient_complete_garbage_returns_all_missing():
    parser = ResultParser()
    config = {"ai_fields": [{"field": "section_a"}]}
    parsed, issues = parser.parse_json_lenient("totally garbage", config)
    assert parsed is None
    assert len(issues) == 1
    assert issues[0]["field"] == "section_a"


def test_parse_json_lenient_empty_content_raises():
    parser = ResultParser()
    with pytest.raises(ResultParseError):
        parser.parse_json_lenient("")


# ── ResultSchemaMismatch ──────────────────────────────────────────────


def test_parse_and_validate_json_raises_schema_mismatch_on_extra_column():
    """表头多出字段 → 抛 ResultSchemaMismatch（可修复）。"""
    parser = ResultParser()
    config = {
        "ai_fields": [
            {
                "field": "section_a",
                "table_schemas": [{"headers": ["作者", "版本号"]}],
            }
        ]
    }
    bad_json = json.dumps(
        {"section_a": {"rows": [{"作者": "x", "版本号": "v1", "extra": "bad"}]}},
        ensure_ascii=False,
    )
    with pytest.raises(ResultSchemaMismatch) as exc_info:
        parser.parse_and_validate_json(bad_json, config)
    assert "section_a" in exc_info.value.offending_fields


def test_parse_and_validate_json_raises_schema_mismatch_on_missing_field():
    parser = ResultParser()
    config = {"ai_fields": [{"field": "section_a"}]}
    with pytest.raises(ResultSchemaMismatch) as exc_info:
        parser.parse_and_validate_json('{"section_other": {}}', config)
    assert "section_a" in exc_info.value.offending_fields
    assert exc_info.value.offending_fields["section_a"]["kind"] == "missing_field"


def test_parse_and_validate_json_raises_schema_mismatch_on_extra_field():
    parser = ResultParser()
    config = {"ai_fields": [{"field": "section_a"}]}
    bad_json = '{"section_a": {}, "section_b_extra": {}}'
    with pytest.raises(ResultSchemaMismatch) as exc_info:
        parser.parse_and_validate_json(bad_json, config)
    assert "section_b_extra" in exc_info.value.offending_fields
    assert exc_info.value.offending_fields["section_b_extra"]["kind"] == "extra_field"


def test_parse_and_validate_json_rejects_table_value_without_template_table():
    """A prose-only template section must never receive a Word table value."""
    parser = ResultParser()
    config = {"ai_fields": [{"field": "section_15", "table_schemas": []}]}

    with pytest.raises(ResultSchemaMismatch) as exc_info:
        parser.parse_and_validate_json(
            '{"section_15": [{"测试点": "支付成功"}]}', config
        )

    issue = exc_info.value.offending_fields["section_15"]
    assert issue["kind"] == "schema_mismatch"
    assert "表格" in issue["messages"][0]


def test_parse_and_validate_json_raises_base_for_json_syntax_error():
    """JSON 语法错抛基类 ResultParseError（不可修复）。"""
    parser = ResultParser()
    config = {"ai_fields": [{"field": "section_a"}]}
    with pytest.raises(ResultParseError) as exc_info:
        parser.parse_and_validate_json('{"section_a": ', config)
    # 必须不是 ResultSchemaMismatch 子类
    assert type(exc_info.value) is ResultParseError


def test_result_schema_mismatch_is_result_parse_error_subclass():
    """向后兼容：所有捕获 ResultParseError 的代码继续生效。"""
    assert issubclass(ResultSchemaMismatch, ResultParseError)
    assert ResultSchemaMismatch("x")  # 基础构造不应抛错
