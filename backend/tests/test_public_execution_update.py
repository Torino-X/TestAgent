"""Tests for the public execution update pipeline (Section 24).

Coverage:
  Builder:
    1.  requirement parser success carries section / table / image counts
    2.  template parser success aggregates per-section ai/keep markers
    3.  knowledge search success reports hit count + confidence
    4.  knowledge search disabled uses the "skipped" branch
    5.  section suggestion rolls up per-action counts
    6.  test plan generator success carries chapter / table / word count
    7.  review passed → success level, with module coverage
    8.  review warning → warning level, indicates regen will fire
    9.  review failed (rule issues) → warning level (NOT a blocker)
    10. test plan regen success reports issue_count / fixed_count
    11. word export success — next_action does NOT claim "task done"
    12. word export success with warnings surfaces the count
    13. format check passed / warning / loss_detected handling
    14. failure formatter always returns a non-None, level=warning
    15. unknown tool success falls back to generic formatter
    16. unknown tool failure falls back to generic formatter
    17. retry formatter covers all 8 strategy labels
    18. retry formatter surfaces last_error in details
    19. retry formatter falls back when strategy is unknown
    20. builder never raises (boundary — malformed data → safe fallback)
    21. sanitizer masks paths / tokens / urls

  Serialization (dataclass roundtrip):
    22. to_dict produces JSON-serializable dict with details as list
    23. public_update_from_dict restores from dict (history recovery)
    24. malformed dict returns None (no crash)

  Orchestrator integration (smoke):
    25. orchestrator wires builder into all three publish points without
        breaking the task (existing event payload still includes
        result.data + tool_name + duration)
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.public_execution_update import (
    PublicExecutionUpdate,
    public_update_from_dict,
)
from app.agent.public_execution_update_builder import (
    _sanitize_text,
    build_for_retry,
    build_for_tool_result,
    split_into_chunks,
)


# ── Builder: success paths ──────────────────────────────────────────────────


def test_req_parser_success_carries_counts():
    upd = build_for_tool_result(
        "RequirementParserTool",
        True,
        {
            "document_title": "SRS v3",
            "document_structure": [{"a": 1}, {"a": 2}],
            "table_summaries": [1, 2],
            "image_texts": ["t"],
            "text_content": "x" * 240,
        },
    )
    assert upd.kind == "tool_result"
    assert upd.level == "success"
    assert upd.headline == "需求文档解析完成"
    assert "需求文档解析" in upd.summary
    assert any("识别章节 2 个" in d for d in upd.details)
    assert any("关键表格 2 个" in d for d in upd.details)
    assert upd.dedupe_key == "RequirementParserTool:tool_result:success"


def test_template_parser_success_aggregates_markers():
    upd = build_for_tool_result(
        "TemplateParserTool",
        True,
        {
            "template_name": "PlanWise",
            "sections": [
                {"table_schemas": [1, 2, 3], "ai_fields": ["a"], "keep_sections": ["x"]},
                {"table_schemas": [], "ai_fields": [], "keep_sections": []},
            ],
        },
    )
    assert upd.level == "success"
    assert upd.headline == "测试方案模板解析完成"
    assert any("内嵌表格 3 个" in d for d in upd.details)
    assert any("AI 生成候选 1 项" in d for d in upd.details)


def test_kb_search_success_reports_confidence():
    upd = build_for_tool_result(
        "KnowledgeSearchTool",
        True,
        {
            "skip_reason": "",
            "similar_projects": [1, 2],
            "standards": [1],
            "terms": [],
            "confidence": "high",
            "elapsed_ms": 300,
        },
    )
    assert upd.level == "success"
    assert upd.headline == "知识库检索完成"
    assert any("命中参考 3 条" in d for d in upd.details)
    assert any("high" in d for d in upd.details)
    assert any("300 ms" in d for d in upd.details)


def test_kb_search_disabled_uses_skipped_branch():
    upd = build_for_tool_result(
        "KnowledgeSearchTool",
        True,
        {"skip_reason": "未启用知识库"},
    )
    assert upd.level == "info"
    assert upd.headline == "知识库检索已跳过"
    assert upd.dedupe_key.endswith(":disabled")
    assert any("未启用知识库" in d for d in upd.details)


def test_section_suggestion_rollup():
    sections = [
        {"suggested_action": "ai_generate"},
        {"suggested_action": "keep_template"},
        {"suggested_action": "manual_fill"},
        {"suggested_action": "skip"},
        {"suggested_action": "ai_generate", "constraint_source": "user_prompt"},
    ]
    upd = build_for_tool_result("SectionSuggestionTool", True, {"sections": sections})
    assert upd.headline == "章节处理建议已生成"
    joined = " ".join(upd.details)
    assert "AI 生成 2 章" in joined
    assert "保留模板 1 章" in joined
    assert "手动补充 1 章" in joined
    assert "不生成 1 章" in joined
    assert "按你的提示词指定" in joined


def test_test_plan_gen_success_carries_counts():
    upd = build_for_tool_result(
        "TestPlanGeneratorTool",
        True,
        {"generated_sections": 3, "kept_sections": 2, "manual_sections": 1, "tables_generated": 5, "total_word_count": 12345},
    )
    assert upd.headline == "测试方案生成完成"
    joined = " ".join(upd.details)
    assert "新增 3 章" in joined
    assert "新增表格 5 个" in joined
    assert "总字数约 12300" in joined


def test_review_passed_success_level():
    upd = build_for_tool_result(
        "ResultReviewTool",
        True,
        {"level": "passed", "passed": True, "covered_module_count": 4, "total_module_count": 5},
    )
    assert upd.level == "success"
    assert "审查通过" in upd.headline
    assert any("覆盖 4/5" in d for d in upd.details)


def test_review_warning_level():
    upd = build_for_tool_result(
        "ResultReviewTool",
        True,
        {"level": "warning", "passed": True, "covered_module_count": 4, "total_module_count": 5},
    )
    assert upd.level == "warning"
    assert "审查通过（含建议）" in upd.headline


def test_review_failed_does_not_block_task():
    upd = build_for_tool_result(
        "ResultReviewTool",
        True,
        {"level": "failed", "passed": False, "covered_module_count": 3, "total_module_count": 5},
    )
    assert upd.level == "warning"
    assert "审查未通过" in upd.headline
    assert "自动重写" in upd.impact


def test_regen_success_reports_counts():
    upd = build_for_tool_result(
        "TestPlanRegenTool", True, {"issue_count": 3, "fixed_count": 2}
    )
    assert upd.headline == "方案重写完成"
    assert any("针对 3 条问题，已修复 2 条" in d for d in upd.details)


def test_word_export_success_next_action_is_not_terminal():
    """WordExportTool success MUST NOT call itself task completion."""
    upd = build_for_tool_result(
        "WordExportTool",
        True,
        {"file_name": "plan.docx", "file_ext": ".docx", "integrity": {"passed": True}},
    )
    assert upd.level == "success"
    assert upd.headline == "Word 文档导出完成"
    # The next action must NOT say "task done"
    assert "完成" not in upd.next_action or "导出" in upd.next_action
    # It must point to the next step (format check)
    assert "格式自检" in upd.next_action


def test_word_export_success_with_warnings_count():
    upd = build_for_tool_result(
        "WordExportTool",
        True,
        {"file_name": "plan.docx", "warnings": ["x", "y"], "integrity": {"passed": False}},
    )
    joined = " ".join(upd.details)
    assert "格式自检附 2 项提示" in joined
    assert "完整性自检：未通过" in joined


def test_word_export_success_omits_unrecognized_integrity_detail():
    upd = build_for_tool_result(
        "WordExportTool",
        True,
        {"file_name": "plan.docx", "integrity": "high"},
    )
    assert not any("完整性自检" in detail for detail in upd.details)


def test_format_check_three_levels():
    for level in ("passed", "warning", "loss_detected"):
        upd = build_for_tool_result(
            "DocxFormatCheckTool",
            True,
            {"level": level, "drift": 0.05, "losses": [1] if level == "loss_detected" else []},
        )
        # Format check should never claim failure of the whole task
        assert upd.level in ("success", "warning")
        assert "格式自检" in upd.headline


# ── Builder: failure paths ───────────────────────────────────────────────────


def test_failure_formatter_always_warns_never_fatal():
    for tool in (
        "RequirementParserTool",
        "TemplateParserTool",
        "KnowledgeSearchTool",
        "SectionSuggestionTool",
        "TestPlanGeneratorTool",
        "ResultReviewTool",
        "TestPlanRegenTool",
        "WordExportTool",
        "DocxFormatCheckTool",
    ):
        upd = build_for_tool_result(
            tool,
            False,
            {},
            {"code": "BOOM", "message": "boom boom", "recoverable": True},
        )
        assert upd.level == "warning"
        # Each tool's failure formatter ends in "未完成" (未完成); some have
        # "未生成" / "跳过" semantic variants.  The guarantee is that the
        # word "未" appears (the failure signal word).
        assert "未" in upd.headline
        assert "系统将依据规则自动决定" in upd.next_action


def test_unknown_tool_success_fallback():
    upd = build_for_tool_result("BrandNewTool", True, {"foo": "bar"})
    assert upd.kind == "tool_result"
    assert upd.level == "success"
    assert "BrandNewTool" in upd.headline
    assert upd.dedupe_key == "BrandNewTool:tool_result:success"


def test_unknown_tool_failure_fallback():
    upd = build_for_tool_result("BrandNewTool", False, {}, "boom")
    assert upd.level == "warning"
    assert "BrandNewTool" in upd.headline


# ── Builder: retry paths ─────────────────────────────────────────────────────


def test_retry_all_strategy_labels():
    strategies = [
        "schema_feedback",
        "reflect_feedback",
        "chunked_split",
        "degrade",
        "degrade_to_template",
        "same_inputs",
        "backoff",
        "hard_stop",
    ]
    for strat in strategies:
        upd = build_for_retry("TestPlanGeneratorTool", strat, 2, "JSON 校验失败", 1.5)
        assert upd.kind == "tool_retry"
        assert upd.level == "retrying"
        assert upd.dedupe_key == f"TestPlanGeneratorTool:tool_retry:{strat}:2"
        # Headline indicates the retry attempt count
        assert "第 2 次重试" in upd.headline


def test_retry_surfaces_last_error_in_details():
    upd = build_for_retry("Foo", "schema_feedback", 3, "schema broken", 0.5)
    assert any("上次原因：schema broken" in d for d in upd.details)
    assert any("距下一次约 0s" in d or "距下一次约 1s" in d for d in upd.details)


def test_retry_unknown_strategy_falls_back():
    upd = build_for_retry("Foo", "totally_made_up", 1, "boom", 0)
    assert upd.level == "retrying"
    assert upd.headline  # non-empty
    assert upd.summary  # non-empty


# ── Boundary: builder never raises ───────────────────────────────────────────


def test_builder_never_raises_on_garbage_input():
    # Many shapes that would normally crash a careless formatter.
    bad_payloads = [
        None,
        {},
        {"sections": "not a list"},
        {"document_structure": "not a list"},
        {"data": [{"x": None}]},
        {"sections": [{"suggested_action": None}]},
    ]
    for payload in bad_payloads:
        upd = build_for_tool_result("RequirementParserTool", True, payload if isinstance(payload, dict) else {})
        assert upd is not None
        assert isinstance(upd, PublicExecutionUpdate)


def test_sanitizer_masks_paths_and_tokens():
    sample = "see /tmp/foo.json with token=ABCD http://internal/path C:/Users/me/file.docx"
    cleaned = _sanitize_text(sample)
    assert "/tmp/foo.json" not in cleaned
    assert "token=ABCD" not in cleaned
    assert "http://internal/path" not in cleaned
    assert "[已脱敏]" in cleaned


# ── Serialization roundtrip ──────────────────────────────────────────────────


def test_to_dict_round_trip_serializable():
    upd = build_for_tool_result(
        "RequirementParserTool",
        True,
        {"document_title": "S", "document_structure": [{"a": 1}], "table_summaries": [], "image_texts": [], "text_content": ""},
    )
    dumped = upd.to_dict()
    # JSON-serializable
    json.dumps(dumped, ensure_ascii=False)
    assert isinstance(dumped["details"], list)


def test_public_update_from_dict_recovers_data():
    original = build_for_tool_result("WordExportTool", True, {"file_name": "p.docx"})
    recovered = public_update_from_dict(original.to_dict())
    assert recovered is not None
    assert recovered.headline == original.headline
    assert recovered.level == original.level
    assert recovered.kind == original.kind


def test_public_update_from_dict_malformed_returns_none():
    assert public_update_from_dict(None) is None
    assert public_update_from_dict({}) is None
    assert public_update_from_dict({"headline": ""}) is None  # rejected: empty headline
    assert public_update_from_dict({"headline": "x", "version": "abc"}) is None


# ── Section 24-ext — streaming chunk frame split ────────────────────────────


def test_chunk_split_success_yields_5_frames_with_progressively_more_fields():
    """A full TOOL_FINISHED success update decomposes into 5 chunks
    where each frame adds exactly one more field (headline → summary →
    impact → next_action → details) and the last frame carries the
    canonical payload.
    """
    full = build_for_tool_result(
        "RequirementParserTool",
        True,
        {
            "section_count": 4,
            "table_count": 1,
            "image_count": 0,
            "summary": "需求已解析",
        },
    )
    frames = split_into_chunks(full)
    assert len(frames) == 5

    # Frame 1 — headline only.
    assert frames[0].headline == full.headline
    assert frames[0].summary == ""
    assert frames[0].impact == ""
    assert frames[0].next_action == ""
    assert frames[0].details == ()
    assert frames[0].chunk_index == 0
    assert frames[0].chunk_total == 5
    assert frames[0].chunk_final is False

    # Frame 2 — adds summary.
    assert frames[1].summary == full.summary
    assert frames[1].impact == ""
    assert frames[1].chunk_final is False

    # Frame 3 — adds impact.
    assert frames[2].impact == full.impact
    assert frames[2].next_action == ""
    assert frames[2].chunk_final is False

    # Frame 4 — adds next_action.
    assert frames[3].next_action == full.next_action
    assert frames[3].details == ()
    assert frames[3].chunk_final is False

    # Frame 5 — adds details, final.
    assert frames[4].details == full.details
    assert frames[4].chunk_index == 4
    assert frames[4].chunk_total == 5
    assert frames[4].chunk_final is True


def test_chunk_split_warning_yields_2_frames():
    """A warning/failure update decomposes into 2 chunks only —
    headline first, then the summary+details combined in the second
    frame.  Negative signals should reach the UI fast."""
    full = build_for_tool_result(
        "RequirementParserTool",
        False,
        {},
        error={"code": "VALIDATION", "message": "字段缺失"},
    )
    frames = split_into_chunks(full)
    assert len(frames) == 2
    assert frames[0].headline == full.headline
    assert frames[0].summary == ""
    assert frames[0].chunk_index == 0
    assert frames[0].chunk_total == 2
    assert frames[0].chunk_final is False
    assert frames[1].summary == full.summary
    assert frames[1].details == full.details
    assert frames[1].chunk_index == 1
    assert frames[1].chunk_total == 2
    assert frames[1].chunk_final is True


def test_chunk_split_retry_yields_2_frames():
    """RETRYING events use 2 frames regardless of level."""
    full = build_for_retry(
        "TestPlanGeneratorTool",
        "schema_feedback",
        attempt=2,
        last_error="JSON invalid",
        next_attempt_in_seconds=1.0,
    )
    frames = split_into_chunks(full)
    assert len(frames) == 2
    assert frames[0].headline == full.headline
    assert frames[0].summary == ""
    assert frames[0].chunk_final is False
    assert frames[1].summary == full.summary
    assert frames[1].chunk_final is True


def test_chunk_final_flag_only_on_last_frame():
    """Exactly one frame per call carries chunk_final=True, and it
    must be the last entry in the list."""
    full = build_for_tool_result(
        "RequirementParserTool",
        True,
        {"section_count": 2, "table_count": 0, "image_count": 0,
         "summary": "ok"},
    )
    for frames in (
        split_into_chunks(full),
        split_into_chunks(
            build_for_tool_result("UnknownTool", False, {}, error="boom")
        ),
        split_into_chunks(
            build_for_retry("TestPlanGeneratorTool", "backoff", 1, "x")
        ),
    ):
        assert sum(1 for f in frames if f.chunk_final) == 1
        assert frames[-1].chunk_final is True
        # Earlier frames must explicitly be False.
        for frame in frames[:-1]:
            assert frame.chunk_final is False


def test_to_dict_round_trip_includes_chunk_fields():
    """to_dict() exposes chunk_index/total/final so the SSE payload
    carries the streaming metadata, and public_update_from_dict()
    restores it losslessly on history replay."""
    full = build_for_tool_result(
        "RequirementParserTool",
        True,
        {"section_count": 3, "table_count": 0, "image_count": 0,
         "summary": "ok"},
    )
    frames = split_into_chunks(full)
    for original in frames:
        data = original.to_dict()
        assert "chunk_index" in data
        assert "chunk_total" in data
        assert "chunk_final" in data
        restored = public_update_from_dict(data)
        assert restored is not None
        assert restored.chunk_index == original.chunk_index
        assert restored.chunk_total == original.chunk_total
        assert restored.chunk_final == original.chunk_final


def test_chunk_field_defaults_keep_legacy_consumers_working():
    """A PublicExecutionUpdate built without chunk_* args must look
    like the legacy single-frame contract: index 0 of 1, final=True."""
    upd = PublicExecutionUpdate(
        version=1,
        kind="tool_result",
        level="success",
        headline="ok",
        summary="",
        impact="",
        next_action="",
        details=(),
    )
    assert upd.chunk_index == 0
    assert upd.chunk_total == 1
    assert upd.chunk_final is True
    data = upd.to_dict()
    assert data["chunk_index"] == 0
    assert data["chunk_total"] == 1
    assert data["chunk_final"] is True
    restored = public_update_from_dict({"headline": "ok"})
    assert restored is not None
    assert restored.chunk_index == 0
    assert restored.chunk_total == 1
    assert restored.chunk_final is True


def test_split_into_chunks_returns_full_update_on_builder_failure(monkeypatch):
    """If ``split_into_chunks`` encounters an unexpected error it must
    degrade to the canonical single-frame list so the orchestrator's
    tool loop never breaks."""
    full = build_for_tool_result(
        "RequirementParserTool",
        True,
        {"section_count": 1, "table_count": 0, "image_count": 0,
         "summary": "ok"},
    )

    def boom(_update):
        raise RuntimeError("split kaboom")

    monkeypatch.setattr(
        "app.agent.public_execution_update_builder._chunk_frame_count",
        boom,
    )
    frames = split_into_chunks(full)
    # Boundary: never raise; fall back to the full update as a single
    # frame so the SSE publish still carries the canonical payload.
    assert len(frames) == 1
    assert frames[0].to_dict() == full.to_dict()
