"""测试故障注入模块单测。

验证 ``apply_test_faults`` / ``apply_format_loss_faults`` 在各种 env 配置下的行为:
  * FAULT_ENABLED=0 → 所有故障关闭（零影响）
  * APP_ENV=production → 忽略 FAULT_* 开关
  * 每种故障类型独立触发 → 对 raw_json / WordExporter 的修改正确
  * 生产环境保护 + 无 AI_FIELDS 时的兜底路径
  * ``apply_format_loss_faults`` 模拟书签丢失,触发 ``format_loss_review`` 路径
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from app.fault_injection_gateway import (
    POINT_AFTER_TEST_PLAN_LLM_RAW_JSON,
    maybe_inject_fault,
)
from app.test_faults import apply_format_loss_faults, apply_test_faults


# ── 测试 fixtures ───────────────────────────────────────────────────────

_SAMPLE_GENERATION_CONFIG = {
    "ai_fields": [
        {
            "field": "section_a",
            "title": "项目概述",
            "table_indexes": [0],
            "table_schemas": [
                {"headers": ["作者", "版本号", "评审日期"]},
            ],
        },
        {"field": "section_b", "title": "测试策略"},
        {"field": "section_c", "title": "交付说明"},
    ],
}


def _sample_json() -> str:
    """构造一个包含 table 的合法 JSON（3 个顶层 section）。"""
    return json.dumps(
        {
            "section_a": [
                [
                    {"作者": "张三", "版本号": "v1.0", "评审日期": "2026-01-01"},
                    {"作者": "李四", "版本号": "v1.1", "评审日期": "2026-01-02"},
                ],
            ],
            "section_b": [
                [
                    {"测试类型": "功能测试", "测试范围": "这是一段很长的测试策略描述文本，长度超过50个字符以触发清空操作的条件判断，用于测试故障注入模块的空值清空功能，确保后续ResultReviewTool能检测到空内容并触发修复流程。"},
                ],
            ],
            "section_c": "第三章节内容，用于缺失测试。",
        },
        ensure_ascii=False,
        indent=2,
    )


def _dev_env(**overrides) -> dict[str, str]:
    """构造 development 环境 + FAULT_ENABLED=1 的 env dict，供触发故障的测试使用。"""
    base = {"APP_ENV": "development", "FAULT_ENABLED": "1"}
    base.update(overrides)
    return base


def _new_fault_env(*scenarios: str, **overrides) -> dict[str, str]:
    """构造新版 FAULT_INJECTION_* 环境。"""
    base = {
        "APP_ENV": "development",
        "FAULT_INJECTION_ENABLED": "1",
        "FAULT_INJECTION_SCENARIOS": ",".join(scenarios),
    }
    base.update(overrides)
    return base


# ── 总开关 ──────────────────────────────────────────────────────────────


def test_all_disabled_returns_unchanged():
    """FAULT_ENABLED=0 → 所有故障关闭，原值直通。"""
    raw = _sample_json()
    with patch.dict(os.environ, _dev_env(FAULT_ENABLED="0", FAULT_SCHEMA_HEADER_MISMATCH="1")):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    assert result == raw


def test_fault_enabled_master_switch_activates_faults():
    """FAULT_ENABLED=1 且有具体开关 → 触发故障。"""
    raw = _sample_json()
    with patch.dict(os.environ, _dev_env(FAULT_SCHEMA_HEADER_MISMATCH="1")):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    payload = json.loads(result)
    headers = list(payload["section_a"][0][0].keys())
    assert any(h.startswith("TEST_FAULT_") for h in headers)


def test_production_env_ignores_faults():
    """APP_ENV=production → 即使 FAULT_ENABLED=1 + FAULT_*=1 也不生效。"""
    raw = _sample_json()
    with patch.dict(os.environ, {"FAULT_ENABLED": "1", "FAULT_SCHEMA_HEADER_MISMATCH": "1", "APP_ENV": "production"}):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    assert result == raw


def test_prod_with_true_string_ignored():
    """APP_ENV=prod → 也忽略。"""
    raw = _sample_json()
    with patch.dict(os.environ, {"FAULT_ENABLED": "1", "FAULT_MISSING_SECTION": "true", "APP_ENV": "prod"}):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    assert result == raw


def test_first_enabled_fault_is_chosen():
    """多个开关同时开启时，只触发第一个。"""
    raw = _sample_json()
    with patch.dict(os.environ, _dev_env(FAULT_SCHEMA_HEADER_MISMATCH="1", FAULT_MISSING_SECTION="1")):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    payload = json.loads(result)
    headers = list(payload["section_a"][0][0].keys())
    assert any(h.startswith("TEST_FAULT_") for h in headers)


# ── schema_header_mismatch ──────────────────────────────────────────────


def test_schema_header_mismatch_corrupts_last_header():
    """把第一个 table 的最后一个 header 改为 TEST_FAULT_xxx。"""
    raw = _sample_json()
    with patch.dict(os.environ, _dev_env(FAULT_SCHEMA_HEADER_MISMATCH="1")):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    payload = json.loads(result)
    headers = list(payload["section_a"][0][0].keys())
    assert "作者" in headers  # 未动
    assert "TEST_FAULT_评审日期" in headers  # 最后一个 header 被替换


# ── missing_section ────────────────────────────────────────────────────


def test_missing_section_removes_penultimate_key():
    """删除倒数第二个顶层 key（section_b）。"""
    raw = _sample_json()
    with patch.dict(os.environ, _dev_env(FAULT_MISSING_SECTION="1")):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    payload = json.loads(result)
    assert "section_a" in payload
    assert "section_c" in payload
    assert "section_b" not in payload


# ── json_truncate ──────────────────────────────────────────────────────


def test_json_truncate_shortens_string():
    """JSON 在 2/3 位置截断（长度减少约 34%）。"""
    raw = _sample_json()
    original_len = len(raw)
    with patch.dict(os.environ, _dev_env(FAULT_JSON_TRUNCATE="1")):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    assert len(result) < original_len
    assert len(result) >= original_len * 0.6


def test_json_truncate_not_applied_to_short_string():
    """非常短的 JSON 不会被截断（长度 < 10）。"""
    short = '{"a": 1}'
    with patch.dict(os.environ, _dev_env(FAULT_JSON_TRUNCATE="1")):
        result = apply_test_faults(short, None)
    assert result == short


# ── empty_section_content ──────────────────────────────────────────────


def test_empty_section_content_clears_longest_string():
    """清空第一个 ai_field section 中最长的字符串字段。"""
    raw = _sample_json()
    with patch.dict(os.environ, _dev_env(FAULT_EMPTY_SECTION_CONTENT="1")):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    payload = json.loads(result)
    assert payload["section_b"][0][0]["测试范围"] == ""


def test_empty_section_content_supports_escaped_new_scenario_id():
    """新版场景名允许使用文档/markdown中常见的转义点号写法。"""
    raw = _sample_json()
    with patch.dict(
        os.environ,
        _new_fault_env(r"result_review\.empty_section_content"),
    ):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    payload = json.loads(result)
    assert payload["section_b"][0][0]["测试范围"] == ""


def test_empty_section_content_clears_object_content_field():
    """真实生成结果常见为对象正文结构,不应只支持表格行清空。"""
    raw = json.dumps(
        {
            "section_b": {
                "content": "这里是测试策略章节正文，应被故障注入清空，以便ResultReviewTool识别空章节并触发RepairAgent。",
                "tables": [],
            },
            "section_c": "交付说明正文，不应被本次注入修改。",
        },
        ensure_ascii=False,
        indent=2,
    )
    config = {
        "ai_fields": [
            {"field": "section_b", "title": "测试策略"},
            {"field": "section_c", "title": "交付说明"},
        ],
    }
    with patch.dict(
        os.environ,
        _new_fault_env(r"result_review\.empty_section_content"),
    ):
        result = apply_test_faults(raw, config)
    payload = json.loads(result)
    assert payload["section_b"]["content"] == ""
    assert payload["section_c"] == "交付说明正文，不应被本次注入修改。"


def test_empty_section_content_no_effect_without_config():
    """无 generation_config 时返回原值（兜底）。"""
    raw = _sample_json()
    with patch.dict(os.environ, _dev_env(FAULT_EMPTY_SECTION_CONTENT="1")):
        result = apply_test_faults(raw, None)
    assert result == raw


# ── section_invalid_value ──────────────────────────────────────────────


def test_section_invalid_value_replaces_first_section():
    """把第一个顶层 key 的值从 array 替换为字符串。"""
    raw = _sample_json()
    with patch.dict(os.environ, _dev_env(FAULT_SECTION_INVALID_VALUE="1")):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    payload = json.loads(result)
    assert payload["section_a"] == "invalid_value_not_expected_type"


# ── JSON 回填契约异常场景 ───────────────────────────────────────────────


def test_extra_table_without_placeholder_generates_unexpected_table():
    """模板无表格占位的章节返回表格数据,应能触发 ResultReview backfill_contract。"""
    raw = _sample_json()
    with patch.dict(
        os.environ,
        _new_fault_env("result_review.extra_table_without_placeholder"),
    ):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    payload = json.loads(result)
    assert isinstance(payload["section_b"], list)
    assert isinstance(payload["section_b"][0], dict)
    assert "异常列A" in payload["section_b"][0]


def test_multiple_tables_for_single_placeholder_generates_two_tables():
    """模板只声明一张表时,人为生成两张表。"""
    raw = _sample_json()
    with patch.dict(
        os.environ,
        _new_fault_env("result_review.multiple_tables_for_single_placeholder"),
    ):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    payload = json.loads(result)
    assert isinstance(payload["section_a"], list)
    assert len(payload["section_a"]) == 2
    assert payload["section_a"][0][0]["作者"] == "first table"
    assert payload["section_a"][1][0]["作者"] == "second table"


def test_missing_table_for_placeholder_removes_required_table_data():
    """模板声明表格占位时,人为把章节改成非表格内容。"""
    raw = _sample_json()
    with patch.dict(
        os.environ,
        _new_fault_env("result_review.missing_table_for_placeholder"),
    ):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    payload = json.loads(result)
    assert payload["section_a"] == "TEST_FAULT_missing_table_data"


def test_table_row_not_object_generates_invalid_table_row_shape():
    """表格行不是对象时,应作为 JSON 回填契约异常进入审查/修复。"""
    raw = _sample_json()
    with patch.dict(
        os.environ,
        _new_fault_env("result_review.table_row_not_object"),
    ):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    payload = json.loads(result)
    assert payload["section_a"] == [["TEST_FAULT_row_is_not_object"]]


def test_fault_injection_task_id_filter_blocks_mismatched_task():
    """设置 FAULT_INJECTION_TASK_IDS 后,非目标 task 不注入。"""
    raw = _sample_json()
    with patch.dict(
        os.environ,
        _new_fault_env(
            "result_review.extra_table_without_placeholder",
            FAULT_INJECTION_TASK_IDS="task_allowed",
        ),
    ):
        result = maybe_inject_fault(
            POINT_AFTER_TEST_PLAN_LLM_RAW_JSON,
            raw,
            context={
                "task_id": "task_blocked",
                "generation_config": _SAMPLE_GENERATION_CONFIG,
            },
        )
    assert result == raw


def test_fault_injection_task_id_filter_allows_matching_task():
    """指定 task_id 匹配时才注入,便于本地精确触发异常测试。"""
    raw = _sample_json()
    with patch.dict(
        os.environ,
        _new_fault_env(
            "result_review.extra_table_without_placeholder",
            FAULT_INJECTION_TASK_IDS="task_allowed",
        ),
    ):
        result = maybe_inject_fault(
            POINT_AFTER_TEST_PLAN_LLM_RAW_JSON,
            raw,
            context={
                "task_id": "task_allowed",
                "generation_config": _SAMPLE_GENERATION_CONFIG,
            },
        )
    payload = json.loads(result)
    assert isinstance(payload["section_b"], list)


# ── 兜底 / 边界 ─────────────────────────────────────────────────────────


def test_malformed_json_returns_original_on_fault_error():
    """如果 fault 本身失败（如 parse 失败），返回原值（不 crash）。"""
    with patch.dict(os.environ, _dev_env(FAULT_SCHEMA_HEADER_MISMATCH="1")):
        result = apply_test_faults("not valid json at all {{{{", None)
    assert result == "not valid json at all {{{{"


def test_env_case_insensitive():
    """env var 值大小写不敏感（用 "TRUE" 也应生效）。"""
    raw = _sample_json()
    with patch.dict(os.environ, _dev_env(FAULT_SCHEMA_HEADER_MISMATCH="TRUE")):
        result = apply_test_faults(raw, _SAMPLE_GENERATION_CONFIG)
    payload = json.loads(result)
    headers = list(payload["section_a"][0][0].keys())
    assert any(h.startswith("TEST_FAULT_") for h in headers)


# ── apply_format_loss_faults (Phase 2.9A.X) ─────────────────────────────
#
# 验证 WordExportTool.format_loss_review 路径的模拟注入:
#   * FAULT_ENABLED=0 → 零影响
#   * APP_ENV=production → 忽略
#   * FAULT_FORMAT_LOSS_BOOKMARK_SIMULATE=1 → exporter.pending_format_losses
#     与 warnings 各出现"书签 170 -> 167"
#   * fidelity="low" → 早返路径,跳过注入


def _make_exporter(fidelity: str = "medium") -> MagicMock:
    """构造最小可注入的 exporter mock。"""
    exporter = MagicMock()
    exporter.fidelity = fidelity
    exporter.pending_format_losses = []
    exporter.warnings = []
    return exporter


def test_format_loss_disabled_returns_silently():
    """FAULT_ENABLED=0 → apply_format_loss_faults 不修改 exporter。"""
    exporter = _make_exporter()
    with patch.dict(os.environ, {"APP_ENV": "development", "FAULT_ENABLED": "0"}):
        apply_format_loss_faults(exporter)
    assert exporter.pending_format_losses == []
    assert exporter.warnings == []


def test_format_loss_production_env_blocks_injection():
    """APP_ENV=production → 即使 FAULT_* 开启也不生效。"""
    exporter = _make_exporter()
    with patch.dict(
        os.environ,
        _dev_env(FAULT_FORMAT_LOSS_BOOKMARK_SIMULATE="1"),
        # 覆盖 APP_ENV 让它走 production 路径
    ):
        with patch.dict(os.environ, {"APP_ENV": "production"}):
            apply_format_loss_faults(exporter)
    assert exporter.pending_format_losses == []
    assert exporter.warnings == []


def test_format_loss_bookmark_simulate_injects_loss():
    """FAULT_FORMAT_LOSS_BOOKMARK_SIMULATE=1 → 注入"书签 170 -> 167"。"""
    exporter = _make_exporter(fidelity="medium")
    with patch.dict(
        os.environ,
        _dev_env(FAULT_FORMAT_LOSS_BOOKMARK_SIMULATE="1"),
    ):
        apply_format_loss_faults(exporter)
    assert "书签 170 -> 167" in exporter.pending_format_losses
    assert any(
        "书签 170 -> 167" in str(w) and "已模拟丢失" in str(w)
        for w in exporter.warnings
    )


def test_format_loss_high_fidelity_also_injects():
    """fidelity="high" 同样注入(format_loss review 路径可达)。"""
    exporter = _make_exporter(fidelity="high")
    with patch.dict(
        os.environ,
        _dev_env(FAULT_FORMAT_LOSS_BOOKMARK_SIMULATE="1"),
    ):
        apply_format_loss_faults(exporter)
    assert "书签 170 -> 167" in exporter.pending_format_losses


def test_format_loss_low_fidelity_skips():
    """fidelity="low" 时 WordExporter 提前 return,模拟注入被跳过(避免误报)。"""
    exporter = _make_exporter(fidelity="low")
    with patch.dict(
        os.environ,
        _dev_env(FAULT_FORMAT_LOSS_BOOKMARK_SIMULATE="1"),
    ):
        apply_format_loss_faults(exporter)
    assert exporter.pending_format_losses == []
    assert exporter.warnings == []


def test_format_loss_idempotent_on_repeated_calls():
    """重复调用 apply_format_loss_faults 不会叠加同一 losses。"""
    exporter = _make_exporter(fidelity="medium")
    with patch.dict(
        os.environ,
        _dev_env(FAULT_FORMAT_LOSS_BOOKMARK_SIMULATE="1"),
    ):
        apply_format_loss_faults(exporter)
        apply_format_loss_faults(exporter)
    bookmark_count = sum(
        1 for loss in exporter.pending_format_losses if "书签" in loss
    )
    assert bookmark_count == 1


def test_format_loss_coexists_with_real_losses():
    """模拟损失与 WordExporter 真实检测到的 losses 共存(用于组合测试)。"""
    exporter = _make_exporter(fidelity="medium")
    # 模拟 WordExporter 已经检测到的真实 secondary losses
    exporter.pending_format_losses.append("批注 5 -> 3")
    exporter.warnings.append("批注 5 -> 3(已容忍)")

    with patch.dict(
        os.environ,
        _dev_env(FAULT_FORMAT_LOSS_BOOKMARK_SIMULATE="1"),
    ):
        apply_format_loss_faults(exporter)

    # 真实 losses 不被清掉,模拟 losses 叠加
    assert "批注 5 -> 3" in exporter.pending_format_losses
    assert "书签 170 -> 167" in exporter.pending_format_losses


def test_format_loss_robust_to_missing_attributes():
    """exporter 缺 pending_format_losses / warnings 列表时不 crash,自动创建。"""
    exporter = MagicMock()
    exporter.fidelity = "medium"
    # 故意不设 pending_format_losses / warnings
    del exporter.pending_format_losses
    del exporter.warnings

    with patch.dict(
        os.environ,
        _dev_env(FAULT_FORMAT_LOSS_BOOKMARK_SIMULATE="1"),
    ):
        # 不应抛异常
        apply_format_loss_faults(exporter)
    # 自动创建为空 list
    assert "书签 170 -> 167" in exporter.pending_format_losses
    assert any("书签 170 -> 167" in str(w) for w in exporter.warnings)
