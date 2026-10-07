from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from app.common.result_parser import ResultParseError
from app.integrations.llm_client import LLMClientError
from app.tools.test_plan_generator_tool import TestPlanGeneratorTool
from app.tools.test_plan_regen_tool import TestPlanRegenTool


def test_regen_table_contract_requires_only_the_parser_accepted_raw_array_shape():
    """The model must be instructed to emit the exact shape the parser accepts.

    A table field is an array (or, for multiple template tables, an outer
    array containing one row-array per table).  Wrapper objects such as
    ``{"table": [...]}`` are deliberately not part of this contract.
    """
    target = {
        "field": "schedule",
        "section_id": "body_59_level_1",
        "section": {"title": "12 测试进度计划"},
        "cfg": {
            # Historical template configs often call this an object.  Table
            # schemas, rather than that legacy label, define the JSON shape.
            "type": "object",
            "table_schemas": [
                {"headers": ["阶段", "开始时间", "结束时间"]},
            ],
        },
    }

    contract = TestPlanRegenTool._build_regen_output_contract([target])
    prompt = TestPlanRegenTool._build_prompt(
        [target],
        {"body_59_level_1": [{"message": "缺少表格行"}]},
        {"ai_fields": [target["cfg"] | {"field": "schedule"}]},
    )

    assert '"schedule" MUST be a JSON array' in contract
    assert '"schedule":[{"阶段"' in contract
    assert "Do not wrap a table value in an object" in contract
    assert "value shape: JSON array of row objects" in prompt
    assert "JSON object / array" not in prompt


def test_regen_parser_keeps_rejecting_table_wrapper_objects():
    """Prompt precision must not be replaced with parser permissiveness."""
    target = [{
        "field": "schedule",
        "cfg": {
            "type": "object",
            "table_schemas": [{"headers": ["阶段", "开始时间"]}],
        },
    }]

    with pytest.raises(ResultParseError, match="应返回数组格式"):
        TestPlanRegenTool._parse_regen_payload(
            '{"schedule":{"table":[{"阶段":"测试","开始时间":"2026-01-01"}]}}',
            target,
        )


def test_primary_generator_uses_the_same_strict_table_contract_as_regen():
    field = {
        "field": "schedule",
        "type": "object",
        "table_schemas": [{"headers": ["阶段", "开始时间"]}],
    }

    primary_contract = TestPlanGeneratorTool._build_test_plan_output_contract(
        {"ai_fields": [field]}
    )
    regen_contract = TestPlanRegenTool._build_regen_output_contract(
        [{"field": "schedule", "cfg": field}]
    )

    assert primary_contract == regen_contract
    assert '"schedule" MUST be a JSON array' in primary_contract
    assert '"schedule":[{"阶段"' in primary_contract
    assert "Do not wrap a table value in an object" in primary_contract


@pytest.mark.asyncio
async def test_regeneration_blocks_without_frozen_context_engine_flag():
    context = SimpleNamespace(task_flag_resolver=None)

    with pytest.raises(LLMClientError, match="MIGRATION_CONTEXT_REQUIRED"):
        await TestPlanRegenTool()._generate_regen_json(
            '{"overview": "x"}',
            target_batch=[],
            context=context,
            use_mock=False,
        )


@pytest.mark.asyncio
async def test_regeneration_passes_generated_content_evidence_to_context_engine(monkeypatch):
    """Regen cannot rely on an Artifact: it must explicitly pass current plan Evidence."""
    from app.context_engine import feature_flags
    from app.tools import _mig_routing

    monkeypatch.setattr(
        feature_flags,
        "require_agent_context_migration",
        lambda *_args, **_kwargs: None,
    )
    captured: dict = {}

    async def fake_invoke(**kwargs):
        captured.update(kwargs)
        return None, '{"overview":{"body":"已补全"}}'

    monkeypatch.setattr(_mig_routing, "invoke_via_bridge_or_none", fake_invoke)
    context = SimpleNamespace(
        task_flag_resolver=object(),
        test_plan_content={
            "section_package": {
                "generated_sections": [
                    {
                        "section_id": "body_18_level_1",
                        "field": "overview",
                        "title": "1 项目概述",
                        "content": {"body": "旧内容"},
                    }
                ]
            }
        },
        template_structure={"generation_config": {"ai_fields": []}},
        review_result={"issues": [{"section_id": "body_18_level_1"}]},
    )

    raw = await TestPlanRegenTool()._generate_regen_json(
        "仅修复 overview",
        target_batch=[{
            "field": "overview",
            "cfg": {"table_schemas": []},
        }],
        context=context,
        use_mock=False,
    )

    assert raw
    evidence = captured["task_state_ref"]["context_evidence"]
    assert evidence["generated_content"][0]["source_ref"] == "task:generated:body_18_level_1"
    assert evidence["review_results"][0]["content"]
    assert "Keys must exactly match" in captured["output_contract"]
    assert '"overview"' in captured["output_contract"]


def test_rebuild_config_maps_frontend_fallback_section_id_by_binding_order():
    tool = TestPlanRegenTool()
    ai_field = {
        "field": "test_scope",
        "section_id": "body_42_level_2",
        "title": "3.2 测试范围",
        "type": "object",
    }
    context = SimpleNamespace(
        template_structure={
            "generation_config": {
                "section_bindings": [
                    {
                        "section_id": "body_7_level_1",
                        "field": "overview",
                        "title": "1 项目概述",
                    },
                    {
                        "section_id": "body_42_level_2",
                        "field": "test_scope",
                        "title": "3.2 测试范围",
                    },
                ],
                "ai_fields": [ai_field],
            }
        }
    )

    rebuilt = tool._rebuild_cfg_subset(
        context,
        ["section_2"],
        {
            "issues": [
                {
                    "section_id": "section_2",
                    "message": "用户选择 AI 生成的章节缺失或内容为空",
                }
            ]
        },
    )

    assert len(rebuilt["ai_fields"]) == 1
    assert rebuilt["ai_fields"][0]["section_id"] == "body_42_level_2"
    assert set(rebuilt["ai_fields"][0]["_request_aliases"]) >= {
        "section_2",
        "body_42_level_2",
        "test_scope",
    }


def test_parse_regen_payload_rejects_table_for_prose_only_template_section():
    with pytest.raises(ResultParseError, match="未声明表格占位"):
        TestPlanRegenTool._parse_regen_payload(
            '{"overview": [{"测试点": "支付成功"}]}',
            [{"field": "overview", "cfg": {"type": "object", "table_schemas": []}}],
        )


@pytest.mark.asyncio
async def test_regen_reconciles_partial_agent_subset_with_all_requested_template_fields(monkeypatch):
    """A non-empty agent subset must not make a sibling repair target disappear."""
    tool = TestPlanRegenTool()
    ai_fields = [
        {
            "field": "overview",
            "section_id": "body_1_level_1",
            "title": "1 Overview",
            "type": "object",
        },
        {
            "field": "scope",
            "section_id": "body_2_level_1",
            "title": "2 Scope",
            "type": "object",
        },
    ]
    context = SimpleNamespace(
        test_plan_content={
            "section_package": {
                "generated_sections": [
                    {"section_id": "body_1_level_1", "field": "overview", "title": "1 Overview", "content": {}},
                    {"section_id": "body_2_level_1", "field": "scope", "title": "2 Scope", "content": {}},
                ]
            }
        },
        template_structure={"generation_config": {"ai_fields": ai_fields}},
        task_flag_resolver=None,
        settings_service=None,
        user_internal_id=1,
    )

    async def fake_generate(self, prompt, *, target_batch, context, use_mock):
        return json.dumps(
            {target["field"]: {"body": f"fixed {target['field']}"} for target in target_batch}
        )

    monkeypatch.setattr(TestPlanRegenTool, "_generate_regen_json", fake_generate)

    result = await tool.run(
        {
            "section_ids": ["body_1_level_1", "body_2_level_1"],
            "issues": [
                {"section_id": "body_1_level_1", "message": "repair first"},
                {"section_id": "body_2_level_1", "message": "repair second"},
            ],
            # Simulate the RepairAgent omitting the second field while still
            # sending a non-empty subset.
            "generation_config_subset": {"ai_fields": [ai_fields[0]]},
        },
        context,
    )

    assert result["success"] is True
    assert result["data"]["fixed_count"] == 2
    assert result["data"]["section_ids"] == ["body_1_level_1", "body_2_level_1"]


@pytest.mark.asyncio
async def test_test_plan_regen_tool_rebuilds_config_from_review_issue_evidence(monkeypatch):
    tool = TestPlanRegenTool()
    ai_fields = [
        {
            "field": "overview",
            "section_id": "body_18_level_1",
            "title": "1 项目概述",
            "type": "object",
            "body_start_index": 1,
            "body_end_index": 2,
        }
    ]
    context = SimpleNamespace(
        test_plan_content={"section_package": {"generated_sections": []}},
        template_structure={"generation_config": {"ai_fields": ai_fields}},
        task_flag_resolver=None,
        settings_service=None,
        user_internal_id=1,
    )
    prompts: list[str] = []

    async def fake_generate_regen_json(self, prompt, *, target_batch, context, use_mock):
        prompts.append(prompt)
        assert '"overview"' in prompt
        return json.dumps(
            {
                "overview": {
                    "body": "项目概述已补全，覆盖业务背景、核心流程和测试关注点。"
                }
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        TestPlanRegenTool,
        "_generate_regen_json",
        fake_generate_regen_json,
    )

    envelope = await tool.run(
        {
            "section_ids": ["section_1"],
            "issues": [
                {
                    "issue_id": "missing-overview",
                    "section_id": "body_18_level_1",
                    "field_path": "overview",
                    "message": "用户选择 AI 生成的章节「1 项目概述」缺失或内容为空",
                    "evidence": {
                        "section_id": "body_18_level_1",
                        "field": "overview",
                        "title": "1 项目概述",
                        "aliases": ["section_1"],
                    },
                }
            ],
        },
        context,
    )

    assert envelope["success"] is True, envelope
    assert len(prompts) == 1
    assert envelope["data"]["fixed_count"] == 1
    assert envelope["data"]["section_ids"] == ["body_18_level_1"]


@pytest.mark.asyncio
async def test_test_plan_regen_tool_bulk_repair_chunks_prompt_batches(monkeypatch):
    tool = TestPlanRegenTool()
    section_ids = [f"sec-{idx}" for idx in range(5)]
    ai_fields = [
        {
            "field": f"field_{idx}",
            "section_id": section_id,
            "title": f"章节 {idx}",
            "type": "object",
            "body_start_index": idx,
            "body_end_index": idx + 1,
        }
        for idx, section_id in enumerate(section_ids)
    ]
    context = SimpleNamespace(
        test_plan_content={
            "section_package": {"generated_sections": []},
            "schema_issues": [
                {
                    "kind": "missing_field",
                    "field": entry["field"],
                    "section_id": entry["section_id"],
                    "source_error": "json_truncated",
                }
                for entry in ai_fields
            ],
            "generation_recovery": {
                "kind": "json_truncated",
                "missing_fields": [entry["field"] for entry in ai_fields],
                "missing_section_ids": section_ids,
                "requires_bulk_repair": True,
            },
        },
        template_structure={"generation_config": {"ai_fields": ai_fields}},
        task_flag_resolver=None,
        settings_service=None,
        user_internal_id=1,
    )
    prompts: list[str] = []

    async def fake_generate_regen_json(self, prompt, *, target_batch, context, use_mock):
        prompts.append(prompt)
        fields = re.findall(r'"(field_\d+)"\s*:', prompt)
        return json.dumps(
            {
                field: {"body": f"{field} 已恢复"}
                for field in dict.fromkeys(fields)
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        TestPlanRegenTool,
        "_generate_regen_json",
        fake_generate_regen_json,
    )

    envelope = await tool.run(
        {
            "section_ids": section_ids,
            "issues": [
                {
                    "issue_id": f"iss-{idx}",
                    "section_id": section_id,
                    "message": "JSON 截断缺失",
                }
                for idx, section_id in enumerate(section_ids)
            ],
            "generation_config_subset": {"ai_fields": ai_fields},
            "recovery_mode": "json_truncated",
            "bulk_repair": True,
        },
        context,
    )

    assert envelope["success"] is True
    assert envelope["data"]["fixed_count"] == 5
    assert envelope["data"]["section_ids"] == section_ids
    assert len(prompts) == 2
    assert len(context.test_plan_content["section_package"]["generated_sections"]) == 5
    assert "schema_issues" not in context.test_plan_content
    assert "generation_recovery" not in context.test_plan_content


@pytest.mark.asyncio
async def test_test_plan_regen_tool_splits_failed_bulk_batch(monkeypatch):
    tool = TestPlanRegenTool()
    section_ids = [f"sec-{idx}" for idx in range(3)]
    ai_fields = [
        {
            "field": f"field_{idx}",
            "section_id": section_id,
            "title": f"章节 {idx}",
            "type": "object",
            "body_start_index": idx,
            "body_end_index": idx + 1,
        }
        for idx, section_id in enumerate(section_ids)
    ]
    context = SimpleNamespace(
        test_plan_content={
            "section_package": {"generated_sections": []},
            "schema_issues": [
                {
                    "kind": "missing_field",
                    "field": entry["field"],
                    "section_id": entry["section_id"],
                    "source_error": "json_truncated",
                }
                for entry in ai_fields
            ],
            "generation_recovery": {
                "kind": "json_truncated",
                "missing_fields": [entry["field"] for entry in ai_fields],
                "missing_section_ids": section_ids,
                "requires_bulk_repair": True,
            },
        },
        template_structure={"generation_config": {"ai_fields": ai_fields}},
        task_flag_resolver=None,
        settings_service=None,
        user_internal_id=1,
    )
    calls: list[list[str]] = []

    async def fake_generate_regen_json(self, prompt, *, target_batch, context, use_mock):
        fields = re.findall(r'"(field_\d+)"\s*:', prompt)
        calls.append(fields)
        if len(fields) > 1:
            raise LLMClientError(
                "MIGRATION_INVOKER_EXCEPTION:ContextEngineFailure: "
                "context.selection.required_unmet"
            )
        return json.dumps({fields[0]: {"body": f"{fields[0]} 已恢复"}}, ensure_ascii=False)

    monkeypatch.setattr(
        TestPlanRegenTool,
        "_generate_regen_json",
        fake_generate_regen_json,
    )

    envelope = await tool.run(
        {
            "section_ids": section_ids,
            "issues": [
                {
                    "issue_id": f"iss-{idx}",
                    "section_id": section_id,
                    "message": "JSON 截断缺失",
                }
                for idx, section_id in enumerate(section_ids)
            ],
            "generation_config_subset": {"ai_fields": ai_fields},
            "recovery_mode": "json_truncated",
            "bulk_repair": True,
        },
        context,
    )

    assert envelope["success"] is True
    assert calls == [["field_0", "field_1", "field_2"], ["field_0"], ["field_1"], ["field_2"]]
    assert envelope["data"]["fixed_count"] == 3
    assert envelope["data"]["partial_success"] is False
    assert envelope["warnings"]
    assert "schema_issues" not in context.test_plan_content


@pytest.mark.asyncio
async def test_test_plan_regen_tool_keeps_partial_success_for_re_review(monkeypatch):
    tool = TestPlanRegenTool()
    section_ids = [f"sec-{idx}" for idx in range(3)]
    ai_fields = [
        {
            "field": f"field_{idx}",
            "section_id": section_id,
            "title": f"章节 {idx}",
            "type": "object",
            "body_start_index": idx,
            "body_end_index": idx + 1,
        }
        for idx, section_id in enumerate(section_ids)
    ]
    context = SimpleNamespace(
        test_plan_content={
            "section_package": {"generated_sections": []},
            "schema_issues": [
                {
                    "kind": "missing_field",
                    "field": entry["field"],
                    "section_id": entry["section_id"],
                    "source_error": "json_truncated",
                }
                for entry in ai_fields
            ],
            "generation_recovery": {
                "kind": "json_truncated",
                "missing_fields": [entry["field"] for entry in ai_fields],
                "missing_section_ids": section_ids,
                "requires_bulk_repair": True,
            },
        },
        template_structure={"generation_config": {"ai_fields": ai_fields}},
        task_flag_resolver=None,
        settings_service=None,
        user_internal_id=1,
    )

    async def fake_generate_regen_json(self, prompt, *, target_batch, context, use_mock):
        fields = re.findall(r'"(field_\d+)"\s*:', prompt)
        if len(fields) > 1 or fields == ["field_1"]:
            raise LLMClientError("context.selection.required_unmet")
        return json.dumps({fields[0]: {"body": f"{fields[0]} 已恢复"}}, ensure_ascii=False)

    monkeypatch.setattr(
        TestPlanRegenTool,
        "_generate_regen_json",
        fake_generate_regen_json,
    )

    envelope = await tool.run(
        {
            "section_ids": section_ids,
            "issues": [
                {
                    "issue_id": f"iss-{idx}",
                    "section_id": section_id,
                    "message": "JSON 截断缺失",
                }
                for idx, section_id in enumerate(section_ids)
            ],
            "generation_config_subset": {"ai_fields": ai_fields},
            "recovery_mode": "json_truncated",
            "bulk_repair": True,
        },
        context,
    )

    assert envelope["success"] is True
    assert envelope["data"]["fixed_count"] == 2
    assert envelope["data"]["partial_success"] is True
    assert envelope["data"]["unresolved_section_ids"] == ["sec-1"]
    remaining = context.test_plan_content["schema_issues"]
    assert [issue["field"] for issue in remaining] == ["field_1"]
    assert context.test_plan_content["generation_recovery"]["missing_fields"] == ["field_1"]
