"""Regression tests for test-plan text values and Word backfill."""

from __future__ import annotations

import json

from app.common.word_exporter import WordExporter
from app.tools.test_plan_json_contract import build_test_plan_json_contract


def _rendered_fields(contract: str) -> list[dict[str, object]]:
    return json.loads(contract.rsplit("\n", 1)[-1])


def test_text_field_contract_requires_a_raw_string_not_a_body_wrapper():
    fields = _rendered_fields(
        build_test_plan_json_contract([{"field": "1.1 目的", "type": "object"}])
    )

    assert fields == [
        {
            "field": "1.1 目的",
            "value_kind": "string",
            "exact_value_shape": "完整的章节正文",
        }
    ]


def test_word_exporter_unwraps_legacy_body_without_rendering_its_label():
    blocks = WordExporter()._content_to_blocks({"body": "第一段\n第二段"})

    assert blocks == [("paragraph", "第一段"), ("paragraph", "第二段")]
