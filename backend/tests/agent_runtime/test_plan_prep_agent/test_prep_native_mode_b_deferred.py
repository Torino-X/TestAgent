"""test_prep_native_mode_b_deferred — Mode B native tool-calling 推迟到 Phase 2.4+ (Phase 2.3 §9.13)."""

from __future__ import annotations

import pytest

from app.agent_runtime.preparation.agent_loop import ModeBNotImplemented, run_preparation
from app.agent_runtime.preparation.capabilities import ModelCapabilities


def test_mode_b_not_implemented_raised_directly():
    """_maybe_mode_b 在 native_tool_calling=True 时抛 ModeBNotImplemented。"""
    from app.agent_runtime.preparation.agent_loop import _maybe_mode_b

    caps = ModelCapabilities(native_tool_calling=True)
    with pytest.raises(ModeBNotImplemented) as exc_info:
        _maybe_mode_b(caps)
    assert "Phase 2.4+" in str(exc_info.value)


def test_mode_a_is_default():
    """default ModelCapabilities.native_tool_calling=False → Mode A。"""
    from app.agent_runtime.preparation.agent_loop import _maybe_mode_b

    caps = ModelCapabilities()
    assert caps.native_tool_calling is False
    assert _maybe_mode_b(caps) == "mode_a"


@pytest.mark.asyncio
async def test_run_preparation_with_native_mode_b_raises(
    fake_llm, stub_adapter, runtime_ctx, base_state
):
    """注入 capabilities.native_tool_calling=True → ModeBNotImplemented 立刻抛。"""
    fake_llm.push('{"action":"finish","decision_summary":"x","public_update":"x","confidence":0.5}')

    caps = ModelCapabilities(native_tool_calling=True)
    with pytest.raises(ModeBNotImplemented):
        await run_preparation(
            base_state,
            llm_client=fake_llm,
            tool_adapter=stub_adapter,
            ctx=runtime_ctx,
            capabilities=caps,
        )


def test_resolve_capabilities_conservative_default():
    """resolve_capabilities() 缺省返回 conservative (native=False)。"""
    from app.agent_runtime.preparation.capabilities import resolve_capabilities

    caps = resolve_capabilities(override=None)
    assert caps.native_tool_calling is False


def test_resolve_capabilities_override():
    """resolve_capabilities(override=...) 透传。"""
    from app.agent_runtime.preparation.capabilities import resolve_capabilities

    custom = ModelCapabilities(native_tool_calling=True, provider_label="test-provider")
    caps = resolve_capabilities(override=custom)
    assert caps.native_tool_calling is True
    assert caps.provider_label == "test-provider"


@pytest.mark.skip(reason="Mode B native tool-calling 推迟到 Phase 2.4+ — 仅留 stub 验证")
def test_mode_b_will_use_native_tool_calling():
    """Phase 2.4+ TODO — Mode B 走 LLMClient.generate_with_tools + AgentDecision 适配器。
    此测试现 skip;Phase 2.4 实现后移除 skip 标记。
    """
    pytest.skip("Mode B deferred to Phase 2.4+")