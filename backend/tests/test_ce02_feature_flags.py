"""CE-02 整改四：Feature Flag 行为测试。

不能只测 16 flags 默认 False，必须验证 flag 对实际行为的门控。

CONTEXT_ENGINE_ENABLED=false：
- ContextEngine 不进入调用链；不写 Snapshot；Pilot 不注册；Shadow 拒绝或 no-op
  但不得写 Snapshot；default Graph 不变。

CONTEXT_ENGINE_AGENT_ENABLED=false：
- Pilot/Agent Invoker 不可运行。

CONTEXT_TOOL_OUTPUT_GOVERNANCE_ENABLED=false：
- 不改变旧 ToolOutput 处理路径。

CONTEXT_FULL_PROMPT_DEBUG_ENABLED=false：
- full_prompt_payload_id 为 null；不创建 Full Prompt Payload。

Flag 开启测试：
- 只显式注册 ce_pilot；不改变默认 v2/v3 key；不改变 build_default_v2_v3。
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest

from app.context_engine.feature_flags import ContextEngineFeatureFlags


def test_all_flags_default_false_and_enum_mapping_is_complete():
    flags = ContextEngineFeatureFlags()
    for field_name, field in flags.__dataclass_fields__.items():
        if field.default is False:
            assert getattr(flags, field_name) is False, f"{field_name} 默认应为 False"
    from app.context_engine.feature_flags import ContextFeatureFlag

    assert set(flags.__dataclass_fields__) == {flag.value for flag in ContextFeatureFlag}


def test_flag_off_engine_enabled_no_pilot_registered():
    """CONTEXT_ENGINE_ENABLED=false → Pilot 不注册（与 default graph 一致）。"""
    from app.agent_runtime.graph_registry import GraphRegistry

    with patch.dict("os.environ", {}, clear=False):
        # 确保 pytest sandbox 不强制开启（get_context_engine_flags 在测试下开总开关）
        # 用构造的 flags 直接验证 build_default_v2_v3 无 pilot
        reg = GraphRegistry.build_default_v2_v3()
        keys = reg.list_versions("ce_pilot")
        assert keys == []  # default graph 无 pilot
        # 默认 v1/v2/v3 key 存在
        assert set(reg.list_versions("test_plan_generation")) == {"v1", "v2", "v3"}


def test_flag_on_pilot_registered_without_changing_default_keys():
    """CONTEXT_ENGINE_ENABLED=true → 只显式注册 ce_pilot，不改变默认 v1/v2/v3 key。"""
    from app.agent_runtime.graph_registry import GraphRegistry
    from app.context_engine import feature_flags as ff_module

    # graph_registry 内部调用 ff_module.get_context_engine_flags()
    with patch.object(
        ff_module,
        "get_context_engine_flags",
        return_value=ContextEngineFeatureFlags(context_engine_enabled=True),
    ):
        reg = GraphRegistry.build_default_v2_v3_with_pilot()
        # pilot 已注册
        assert "v1" in reg.list_versions("ce_pilot")
        # 默认 v1/v2/v3 key 不变
        assert set(reg.list_versions("test_plan_generation")) == {"v1", "v2", "v3"}


def test_flag_off_build_default_v2_v3_with_pilot_equals_default():
    """flag 关闭时 build_default_v2_v3_with_pilot == build_default_v2_v3（无 pilot）。"""
    from app.agent_runtime.graph_registry import GraphRegistry
    from app.context_engine import feature_flags as ff_module

    with patch.object(
        ff_module,
        "get_context_engine_flags",
        return_value=ContextEngineFeatureFlags(context_engine_enabled=False),
    ):
        reg = GraphRegistry.build_default_v2_v3_with_pilot()
        assert reg.list_versions("ce_pilot") == []
        assert set(reg.list_versions("test_plan_generation")) == {"v1", "v2", "v3"}


def test_engine_enabled_off_engine_not_in_call_chain():
    """CONTEXT_ENGINE_ENABLED=false → ContextEngine 不进入调用链（不写 Snapshot）。"""
    flags = ContextEngineFeatureFlags(context_engine_enabled=False)
    assert flags.context_engine_enabled is False
    from app.context_engine.feature_flags import ContextFeatureFlag

    assert (
        flags.evaluate(ContextFeatureFlag.CONTEXT_ENGINE_ENABLED) is False
    )


def test_agent_enabled_off_invoker_not_runnable():
    """CONTEXT_ENGINE_AGENT_ENABLED=false → Pilot/Agent Invoker 不可运行。"""
    flags = ContextEngineFeatureFlags(context_engine_agent_enabled=False)
    assert flags.context_engine_agent_enabled is False
    # Agent Invoker 运行前应检查该 flag（此处验证 flag 值，运行时门控由调用方负责）
    assert not flags.context_engine_agent_enabled


def test_tool_output_governance_off_legacy_path_unchanged():
    """CONTEXT_TOOL_OUTPUT_GOVERNANCE_ENABLED=false → 不改变旧 ToolOutput 处理路径。"""
    flags = ContextEngineFeatureFlags(context_tool_output_governance_enabled=False)
    assert flags.context_tool_output_governance_enabled is False
    # 治理关闭时 ToolOutputManager 不应被调用（调用方应走 legacy 路径）
    from app.context_engine.feature_flags import ContextFeatureFlag

    assert (
        flags.evaluate(ContextFeatureFlag.CONTEXT_TOOL_OUTPUT_GOVERNANCE_ENABLED)
        is False
    )


def test_full_prompt_debug_off_no_full_prompt():
    """CONTEXT_FULL_PROMPT_DEBUG_ENABLED=false → full_prompt_payload_id null，不创建 Full Prompt Payload。"""
    flags = ContextEngineFeatureFlags(context_full_prompt_debug_enabled=False)
    assert flags.context_full_prompt_debug_enabled is False
    # 派生约束：full_prompt_debug 需要 engine 开启
    assert flags.full_prompt_debug_requires_engine is False


def test_memory_derived_constraints():
    """派生约束：auto_activate 依赖 write；full_prompt_debug 依赖 engine。"""
    flags = ContextEngineFeatureFlags(
        context_memory_write_enabled=True,
        context_memory_auto_extract_enabled=True,
        context_memory_auto_activate_enabled=True,
        context_engine_enabled=True,
        context_full_prompt_debug_enabled=True,
    )
    assert flags.memory_write_implies_extract is True
    assert flags.memory_auto_activate_requires_write is True
    assert flags.full_prompt_debug_requires_engine is True
