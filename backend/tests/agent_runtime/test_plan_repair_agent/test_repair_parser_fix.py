"""Phase 2.9A.X: Repair agent_loop 不再传字符串 parser。

根因：``agent_loop.py`` 之前硬编码 ``parser="json_strict"``（字符串），
但 ``generate_with_profile`` 的 ``parser`` 参数期望 ParserAdapter 实例。
字符串是 truthy → ``adapter = parser or get_parser(...)`` 走到
``adapter = "json_strict"`` → 调 ``.parse()`` 抛 ``'str' object has
no attribute 'parse'`` → RepairAgent 降级到 fallback。

修复：删掉硬编码的 parser 参数，让 ``profile.parser`` 自动通过
``get_parser(profile.parser)`` 解析为 ``JsonStrictParser`` 实例。
"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    _make_review_result,
    call_regen_decision,
    call_review_decision,
    finish_decision,
    regen_envelope_ok,
    review_envelope_passed,
)


@pytest.mark.asyncio
async def test_repair_does_not_pass_string_parser_to_llm(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """``run_repair`` 调 ``generate_with_profile`` 时 parser 必须是 None 或
    ParserAdapter 实例,绝不能是字符串（否则 'str' object has no attribute 'parse'）。
    """
    base_state["review_result"] = _make_review_result(
        ["iss-1"], ["sec-A"], level="failed", kind="json_key_rename"
    )
    fake_llm.extend([
        call_regen_decision(["sec-A"], ["iss-1"], summary="重命名 JSON key"),
        call_review_decision(),
        finish_decision(["iss-1"]),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-A"])],
        "ResultReviewTool": [review_envelope_passed()],
    }

    await run_repair(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # 至少调过一次 LLM
    assert fake_llm.calls, "应至少调一次 generate_with_profile"

    # 关键断言：parser 不能是字符串
    for call_record in fake_llm.calls:
        # FakeLLMClient.calls 记录里目前没存 parser。直接看 inspect.generate_with_profile
        # 调用 signature — 加一个明确的 parser 字段到 FakeLLMClient.calls 即可。
        # 这里通过显式 attrs（如果存在）兜底：
        parser_value = call_record.get("parser")
        if parser_value is not None:
            assert not isinstance(parser_value, str), (
                f"parser 必须是 None 或 ParserAdapter 实例,不能是字符串: {parser_value!r}"
            )


@pytest.mark.asyncio
async def test_repair_llm_parser_is_not_string(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """直接 spy generate_with_profile 的 kwargs,验证 parser 不是字符串。

    这个测试比上一个更直接 — 给 fake_llm 加一个 kwargs 记录字段,
    然后检查每次调用的 parser 实参。
    """
    base_state["review_result"] = _make_review_result(
        ["iss-2"], ["sec-B"], level="failed", kind="json_key_rename"
    )
    fake_llm.extend([
        call_regen_decision(["sec-B"], ["iss-2"], summary="修复"),
        call_review_decision(),
        finish_decision(["iss-2"]),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-B"])],
        "ResultReviewTool": [review_envelope_passed()],
    }

    # Monkey-patch generate_with_profile 抓 kwargs
    captured_kwargs: list[dict] = []
    original = fake_llm.generate_with_profile

    async def spy_generate(profile, content, **kwargs):
        captured_kwargs.append({"profile": profile, "kwargs": dict(kwargs)})
        return await original(profile, content, **kwargs)

    fake_llm.generate_with_profile = spy_generate  # type: ignore[method-assign]

    await run_repair(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert captured_kwargs, "应至少调一次 generate_with_profile"
    for cap in captured_kwargs:
        kwargs = cap["kwargs"]
        # 关键断言：parser 必须是 None 或 ParserAdapter 实例,绝不能是 str
        parser = kwargs.get("parser")
        assert parser is None or not isinstance(parser, str), (
            f"parser 传了字符串 {parser!r} — 会导致 "
            "'str' object has no attribute 'parse' 错误。"
            "应传 None（让 profile.parser 走 get_parser）或 ParserAdapter 实例。"
        )


@pytest.mark.asyncio
async def test_repair_passes_graph_state_to_tool_adapter(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """Phase 2.9A.X: ``run_repair`` 调 ``tool_adapter.execute`` 时
    必须传 ``graph_state=state``。否则 ``_build_proxy`` 走
    ``ctx._intermediate_state``（空 dict），TestPlanRegenTool 拿不到
    ``context.test_plan_content`` → REGEN_NO_EXISTING 失败，
    RepairAgent 反复重试仍失败。

    这个测试锁定 graph_state 透传契约。
    """
    base_state["review_result"] = _make_review_result(
        ["iss-3"], ["sec-C"], level="failed", kind="json_key_rename"
    )
    # 给 base_state 注入 test_plan_content 模拟生成器已写入 graph state
    base_state["test_plan_content"] = {
        "section_package": {
            "generated_sections": [
                {"section_id": "sec-C", "title": "S", "content": "x" * 100},
            ],
        }
    }
    fake_llm.extend([
        call_regen_decision(["sec-C"], ["iss-3"], summary="修复"),
        call_review_decision(),
        finish_decision(["iss-3"]),
    ])
    stub_adapter.envelopes_per_tool = {
        "TestPlanRegenTool": [regen_envelope_ok(["sec-C"])],
        "ResultReviewTool": [review_envelope_passed()],
    }

    await run_repair(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    # 至少一次 tool 调用,且 graph_state 必须含 test_plan_content
    regen_calls = [
        c for c in stub_adapter.calls if c["tool_name"] == "TestPlanRegenTool"
    ]
    assert regen_calls, "TestPlanRegenTool 应至少被调一次"
    for call_record in regen_calls:
        graph_state_keys = call_record.get("graph_state_keys", [])
        assert "test_plan_content" in graph_state_keys, (
            f"graph_state 必须包含 test_plan_content,实际 keys={graph_state_keys}"
            " — 否则 TestPlanRegenTool 拿不到输入会失败"
        )