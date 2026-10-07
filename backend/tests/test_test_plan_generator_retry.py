"""F023 split + Phase 2.9A.X — TestPlanGeneratorTool contract.

Phase 2.9A.X: F023 整体重试被移除，header/字段 schema 不符改走
``ResultReviewTool`` + ``RepairAgent`` 定点修复。**仅 JSON 语法错**仍
走内部 retry（generic closure prompt）。

This module pins:

  * 表头 / 字段 schema 不符（``ResultSchemaMismatch``）:
      - envelope.data.schema_issues 携带 offending fields
      - 任何 attempt 都不再触发内部 LLM 重试（用户实测：整体重试 3 次仍失败）
      - RepairAgent 走 ``TestPlanRegenTool(section_ids=...)`` 定点修复

  * JSON 语法错（``ResultParseError`` 基类）:
      - 首次（retry_context is None）→ 表面错误，不重试
      - ``strategy == "schema_feedback"`` 第二次起 → ONE retry with
        ``【JSON 闭合要求】`` generic closure prompt（无字段白名单）
      - 其他 strategy（backoff / same_inputs / degrade）→ 不重试
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, List

import pytest

from app.agent.context import AgentContext
from app.agent.retry_policy import RetryContext
from app.common.prompt_builder import PromptBuilder
from app.common.result_parser import (
    ResultParseError,
    ResultParser,
    ResultSchemaMismatch,
)
from app.tools.test_plan_generator_tool import TestPlanGeneratorTool
from app.tools.test_plan_regen_tool import TestPlanRegenTool


# ── Helpers ────────────────────────────────────────────────────────────


class _ScriptedLLM:
    """Async LLM stub returning queued responses per call.

    Records every prompt so tests can assert closure-prompt content.
    """

    def __init__(self, responses: List[str]) -> None:
        if not responses:
            raise ValueError("responses must contain at least one entry")
        self._responses = list(responses)
        self.calls: int = 0
        self.prompts: List[str] = []

    async def generate(self, prompt: str, images=None) -> str:  # noqa: ARG002
        self.calls += 1
        self.prompts.append(prompt)
        idx = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[idx]

    async def continue_generation(self, raw: str, prompt: str) -> str:  # noqa: ARG002
        # 默认续写无动作（truncation 测试可 override）
        return ""


def _make_context(
    *,
    requirement_text: str = "请实现一个用户登录功能",
    sections: list[dict] | None = None,
    generation_config: dict | None = None,
) -> AgentContext:
    sections = sections or [
        {
            "section_id": "section_1",
            "title": "测试用例",
            "level": 1,
            "table_schemas": [
                {
                    "headers": ["用例编号", "用例名称", "前置条件", "操作步骤", "预期结果"],
                },
            ],
        },
    ]
    generation_config = generation_config or {
        "ai_fields": [
            {
                "field": "section_1",
                "title": "测试用例",
                "table_schemas": [
                    {
                        "headers": [
                            "用例编号",
                            "用例名称",
                            "前置条件",
                            "操作步骤",
                            "预期结果",
                        ],
                    },
                ],
            },
        ],
    }
    return AgentContext(
        task_id="task_001",
        conversation_id="conv_001",
        user_id="user_001",
        user_internal_id=1,
        requirement_file_id="file_req",
        template_file_id="file_tpl",
        requirement_analysis={"text_content": requirement_text},
        template_structure={
            "sections": sections,
            "generation_config": generation_config,
        },
        session=SimpleNamespace(),  # not used — _resolve_llm_config short-circuits
    )


def _good_payload_json() -> str:
    return json.dumps(
        {
            "section_1": [
                [
                    {
                        "用例编号": "TC-001",
                        "用例名称": "正常登录",
                        "前置条件": "用户已注册",
                        "操作步骤": "1. 输入账号密码 2. 点击登录",
                        "预期结果": "登录成功",
                    },
                ],
            ],
        },
        ensure_ascii=False,
    )


def _bad_header_payload_json() -> str:
    """Same shape but with an extra column header — schema mismatch."""
    return json.dumps(
        {
            "section_1": [
                [
                    {
                        "用例编号": "TC-001",
                        "用例名称": "正常登录",
                        "前置条件": "用户已注册",
                        "操作步骤": "1. 输入账号密码 2. 点击登录",
                        "预期结果": "登录成功",
                        "EXTRA_COLUMN": "本字段不在模板",
                    },
                ],
            ],
        },
        ensure_ascii=False,
    )


def _truncated_payload_json() -> str:
    """JSON 末尾不闭合 — 模拟 max_tokens 截断。"""
    return (
        '{"section_1": [[{"用例编号": "TC-001", "用例名称": "正常登录"'
    )


def _syntax_error_payload_json() -> str:
    """JSON 语法错（outer 已闭合但内部结构破坏）— 不算截断。"""
    # 缺失字段值的右半部分：``[{"key": value_without_quotes}]`` 是非法的
    return '{"section_1": [{"key": value_without_quotes}]}'


# ── Driver: mirrors the real tool path (truncation → validate → retry) ─


@dataclass
class _ToolDriver:
    tool: TestPlanGeneratorTool
    llm: _ScriptedLLM
    prompt_builder: PromptBuilder = field(default_factory=PromptBuilder)
    result_parser: ResultParser = field(default_factory=ResultParser)

    async def run_with(
        self,
        ctx: AgentContext,
        *,
        retry_context: RetryContext | None = None,
        truncated_response: str | None = None,
        bypass_truncation_check: bool = False,
    ) -> dict:
        """Drive tool path: generate → (continue) → validate → (retry) → build.

        For the new contract:
        - ``truncated_response`` overrides the LLM's response (simulating
          truncation without consuming the queue)
        """
        full_prompt = self.prompt_builder.build(
            user_prompt="请生成测试方案",
            requirement_text=ctx.requirement_analysis["text_content"],
            template_structure={
                "headings": ["测试用例"],
                "table_fields": [
                    "测试用例 表格字段：用例编号 | 用例名称 | 前置条件 | 操作步骤 | 预期结果",
                ],
            },
            template_generation_config=ctx.template_structure["generation_config"],
        )
        if truncated_response is not None:
            # 直接返回 truncated，跳过 scripted LLM
            raw_json = truncated_response
        else:
            raw_json = await self.llm.generate(full_prompt)

        # ── truncation detection & continuation (mirror real tool) ──
        if not bypass_truncation_check and self.result_parser.is_json_truncated(raw_json):
            try:
                continuation = await self.llm.continue_generation(raw_json, full_prompt)
                raw_json = raw_json + continuation
            except Exception:
                pass
            if self.result_parser.is_json_truncated(raw_json):
                # 触发宽容解析
                partial, schema_issues = self.result_parser.parse_json_lenient(
                    raw_json, ctx.template_structure["generation_config"],
                )
                if partial is not None:
                    return {
                        "success": True,
                        "partial": True,
                        "data": {"section_package": partial, "schema_issues": schema_issues},
                    }
                return {
                    "success": True,
                    "partial": True,
                    "data": {"schema_issues": schema_issues or []},
                }

        # ── validate ──
        try:
            self.result_parser.parse_and_validate_json(
                raw_json, ctx.template_structure["generation_config"],
            )
            return {
                "success": True,
                "tool_name": "TestPlanGeneratorTool",
                "summary": "校验通过",
                "data": {"raw_json": raw_json},
            }
        except ResultSchemaMismatch as exc:
            # 新路径：返回 envelope with schema_issues
            issues = self.tool._convert_to_schema_issues(exc.offending_fields)
            return {
                "success": True,
                "tool_name": "TestPlanGeneratorTool",
                "summary": "schema 不符 → schema_issues 透传",
                "data": {"schema_issues": issues},
                "llm_calls": self.llm.calls,
            }
        except ResultParseError as exc:
            # JSON syntax errors may retry once through the Context Engine.
            payload, ok = await self.tool._maybe_retry_with_json_syntax_fix(
                exc=exc,
                context=ctx,
                user_prompt="请生成测试方案",
                template_generation_config=ctx.template_structure["generation_config"],
                task_state_ref={},
                result_parser=self.result_parser,
                retry_context=retry_context,
            )
            if ok:
                return {
                    "success": True,
                    "tool_name": "TestPlanGeneratorTool",
                    "summary": "JSON 闭合后通过",
                    "data": payload,
                }
            return {
                "success": False,
                "tool_name": "TestPlanGeneratorTool",
                "summary": f"校验失败：{exc}",
                "error": {
                    "code": "JSON_VALIDATION_FAILED",
                    "message": str(exc),
                    "recoverable": True,
                },
            }


# ── Schema mismatch: 不再整体重试，走 schema_issues 透传 ──


@pytest.mark.asyncio
async def test_schema_mismatch_is_recovered_by_targeted_regeneration(monkeypatch):
    """Structural output errors are repaired before ResultReview sees a success."""
    tool = TestPlanGeneratorTool()
    ctx = _make_context()
    config = ctx.template_structure["generation_config"]
    parser = ResultParser()
    bad_raw = _bad_header_payload_json()

    with pytest.raises(ResultSchemaMismatch) as caught:
        parser.parse_and_validate_json(bad_raw, config)

    captured: dict[str, Any] = {}

    async def fake_regenerate(self, inputs, context, retry_context=None):  # noqa: ARG001
        captured.update(inputs)
        repaired = parser.parse_and_validate_json(_good_payload_json(), config)
        context.test_plan_content["section_package"] = parser.build_section_package(
            repaired,
            config,
        )
        context.test_plan_content.pop("schema_issues", None)
        return {
            "success": True,
            "data": {
                "partial_success": False,
                "unresolved_section_ids": [],
            },
        }

    monkeypatch.setattr(TestPlanRegenTool, "run", fake_regenerate)

    payload = await tool._recover_schema_mismatch(
        context=ctx,
        raw_json=bad_raw,
        result_parser=parser,
        generation_config=config,
        schema_issues=tool._convert_to_schema_issues(caught.value.offending_fields),
    )

    assert payload == parser.parse_and_validate_json(_good_payload_json(), config)
    assert captured["bulk_repair"] is True
    assert captured["section_ids"] == ["section_1"]
    assert captured["issues"][0]["section_id"] == "section_1"
    assert "schema_issues" not in ctx.test_plan_content


@pytest.mark.asyncio
async def test_schema_mismatch_recovery_fails_closed_when_regenerator_raises(monkeypatch):
    tool = TestPlanGeneratorTool()
    ctx = _make_context()
    config = ctx.template_structure["generation_config"]
    parser = ResultParser()
    bad_raw = _bad_header_payload_json()

    with pytest.raises(ResultSchemaMismatch) as caught:
        parser.parse_and_validate_json(bad_raw, config)

    async def failing_regenerate(self, inputs, context, retry_context=None):  # noqa: ARG001
        raise RuntimeError("context.selection.required_unmet")

    monkeypatch.setattr(TestPlanRegenTool, "run", failing_regenerate)

    payload = await tool._recover_schema_mismatch(
        context=ctx,
        raw_json=bad_raw,
        result_parser=parser,
        generation_config=config,
        schema_issues=tool._convert_to_schema_issues(caught.value.offending_fields),
    )

    assert payload is None


# ── JSON 语法错：仍走 _maybe_retry_with_json_syntax_fix ──


@pytest.mark.asyncio
async def test_json_syntax_first_attempt_no_internal_retry():
    """JSON 语法错（首次 attempt，retry_context is None）→ 表面错误，不重试。"""
    driver = _ToolDriver(
        tool=TestPlanGeneratorTool(),
        llm=_ScriptedLLM([_syntax_error_payload_json()]),
    )
    ctx = _make_context()

    result = await driver.run_with(ctx, retry_context=None)

    assert result["success"] is False
    assert result["error"]["code"] == "JSON_VALIDATION_FAILED"
    assert driver.llm.calls == 1


@pytest.mark.asyncio
async def test_json_syntax_schema_feedback_injects_closure_prompt(monkeypatch):
    """JSON 语法错 + schema_feedback retry → 触发内部重试 + 【JSON 闭合要求】 提示。"""
    driver = _ToolDriver(
        tool=TestPlanGeneratorTool(),
        llm=_ScriptedLLM([_syntax_error_payload_json()]),
    )
    ctx = _make_context()
    retry_ctx = RetryContext(
        attempt=2,
        max_retries=3,
        previous_errors=[{"code": "JSON_VALIDATION_FAILED", "message": "x", "recoverable": True}],
        strategy="schema_feedback",
    )

    from app.tools import _mig_routing

    captured: dict[str, Any] = {}

    async def fake_bridge(**kwargs):
        captured.update(kwargs)
        return None, _good_payload_json()

    monkeypatch.setattr(_mig_routing, "invoke_via_bridge_or_none", fake_bridge)

    result = await driver.run_with(ctx, retry_context=retry_ctx)

    assert result["success"] is True
    assert driver.llm.calls == 1
    assert "【JSON 闭合要求" in captured["current_goal"]
    # 关键断言：不含字段白名单（这是与 F023 的核心区别）
    assert "【表头合规性强制要求" not in captured["current_goal"]


@pytest.mark.asyncio
async def test_json_syntax_backoff_strategy_skips_internal_retry():
    """strategy='backoff' → 不走内部 JSON 闭合重试。"""
    driver = _ToolDriver(
        tool=TestPlanGeneratorTool(),
        llm=_ScriptedLLM([_syntax_error_payload_json(), _good_payload_json()]),
    )
    ctx = _make_context()
    retry_ctx = RetryContext(
        attempt=2,
        max_retries=3,
        previous_errors=[{"code": "JSON_VALIDATION_FAILED", "message": "x", "recoverable": True}],
        strategy="backoff",
    )

    result = await driver.run_with(ctx, retry_context=retry_ctx)

    assert result["success"] is False
    assert driver.llm.calls == 1


# ── Truncation: 走宽容解析路径 ──


@pytest.mark.asyncio
async def test_truncated_response_returns_partial_with_schema_issues():
    """截断 JSON（outer 未闭合）→ 宽容解析返回 (None, schema_issues for all)。"""
    driver = _ToolDriver(
        tool=TestPlanGeneratorTool(),
        llm=_ScriptedLLM(["unused_response"]),  # 不会被用到
    )
    ctx = _make_context()

    result = await driver.run_with(
        ctx, retry_context=None, truncated_response=_truncated_payload_json(),
    )

    assert result["success"] is True
    assert result["partial"] is True
    assert "schema_issues" in result["data"]
    # 截断后宽容解析返回的 schema_issues 应至少含 section_1
    fields = [iss["field"] for iss in result["data"]["schema_issues"]]
    assert "section_1" in fields


# ── Success path ──


@pytest.mark.asyncio
async def test_first_attempt_success_does_not_invoke_retry_helper():
    """首次成功 → 无第二次 LLM 调用。"""
    driver = _ToolDriver(
        tool=TestPlanGeneratorTool(),
        llm=_ScriptedLLM([_good_payload_json()]),
    )
    ctx = _make_context()

    result = await driver.run_with(ctx, retry_context=None)

    assert result["success"] is True
    assert driver.llm.calls == 1
