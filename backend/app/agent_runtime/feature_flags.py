"""LangGraph runtime 的特性开关。

Phase 2.0 默认全部关闭;只有显式 env 标记 (test mode) 或 Phase 2.1+
才会启用 LangGraph 路径。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

EngineType = Literal["langgraph"]


class LangGraphDisabledError(RuntimeError):
    """当 ``langgraph_enabled=False`` 但请求 LangGraph 引擎时抛出。"""


@dataclass(frozen=True)
class AgentRuntimeFeatureFlags:
    """进程级 feature flag。frozen + slots,避免测试中意外变更。

    Phase 2.2 增量:``interrupt_v2_enabled`` 控制 LangGraph 路径是否走
    ``interrupt()`` + ``Command(resume=...)`` (新路径)。默认 ``False``:
    即使 LangGraph 总开关打开,sentinel 路径仍然是兼容默认(Phase 2.1 行为),
    保证生产热路径零变化。

    Phase 2.4 增量:``repair_agent_enabled`` 控制 Review Repair Agent
    动态子图是否接管 review-regen 循环。默认 ``False``;即使总开关打开,
    也需要 env/pytest 显式 opt-in。
    """

    langgraph_enabled: bool = False
    interrupt_v2_enabled: bool = False
    preparation_agent_enabled: bool = False
    repair_agent_enabled: bool = False
    incremental_agent_enabled: bool = False   # Phase 2.5: 增量任务 Agent
    force_in_tests: bool = True
    # Production execution is fail-closed. This is a readiness gate, not an
    # engine selector: every executable task is already LangGraph.
    production_dispatch_enabled: bool = False
    # Phase 2.8C: 动态 Agent API 入口总开关(增量/Repair)。
    # 仅当 True 时 ApiDispatcher.dispatch_incremental_task / dispatch_incremental_resume
    # / dispatch_repair_task 才挂真实入口;False 时显式拒绝执行。
    # 默认 False;env AGENT_RUNTIME_DYNAMIC_AGENT_API_ENABLED=1 显式 opt-in;
    # 测试 sandbox (PYTEST_CURRENT_TEST) auto-on。
    # 守禁令 #31:不允许 dynamic_agent_api 在 production 默认开启。
    dynamic_agent_api_enabled: bool = False
    # Phase 2.9B: public dynamic decision/observation narration.  Kept off by
    # default so existing UI and event traffic remain byte-for-byte compatible.
    phase29b_narrative_enabled: bool = False
    phase29b_preparation_narrative_enabled: bool = False
    phase29b_repair_narrative_enabled: bool = False
    phase29b_incremental_narrative_enabled: bool = False
    # Phase 2.9B.4: LLM-first Tool narrative + task summary. All opt-in,
    # default off — keeps existing event traffic byte-for-byte compatible.
    phase29b_tool_narrative_enabled: bool = False
    phase29b_tool_narrative_blocking_enabled: bool = False
    phase29b_tool_narrative_final_only: bool = True
    phase29b_task_summary_narrative_enabled: bool = False
    phase29b_deterministic_fallback_enabled: bool = True
    phase29b_narrative_stream_persist_enabled: bool = True
    phase29b_narrative_timeout_seconds: int = 45
    phase29b_narrative_repair_attempts: int = 1
    # Phase 2.9C: narrative governance layer over Phase 2.9A / 2.9B.
    # Total switch must be False in production by default — keeps the
    # existing 2.9A/2.9B behaviour byte-for-byte compatible. Sub-flags
    # only matter when the total is True; the shadow flag returns
    # governance metadata without changing the published output.
    phase29c_governance_enabled: bool = False
    phase29c_quality_gate_enabled: bool = True
    phase29c_detail_level_enabled: bool = True
    phase29c_dedup_enabled: bool = True
    phase29c_compression_enabled: bool = True
    phase29c_llm_compression_enabled: bool = False
    phase29c_shadow_mode: bool = False

    def phase29b_narrative_enabled_for(self, agent_name: str) -> bool:
        """Return whether public narration is enabled for one runtime agent.

        The global flag is intentionally a gate for all three rollout flags.
        This keeps the feature opt-in in production and allows each agent to
        be enabled independently during Phase 2.9B verification.
        """
        normalized = (agent_name or "").strip().lower().replace("_", "")
        per_agent = {
            "preparationagent": self.phase29b_preparation_narrative_enabled,
            "repairagent": self.phase29b_repair_narrative_enabled,
            "incrementalagent": self.phase29b_incremental_narrative_enabled,
            "preparation": self.phase29b_preparation_narrative_enabled,
            "repair": self.phase29b_repair_narrative_enabled,
            "incremental": self.phase29b_incremental_narrative_enabled,
        }
        return self.phase29b_narrative_enabled and per_agent.get(normalized, False)

_DEFAULT_FLAGS = AgentRuntimeFeatureFlags()


def get_feature_flags() -> AgentRuntimeFeatureFlags:
    """读取进程级 feature flag。

    优先级:
    1. ``AGENT_RUNTIME_LANGGRAPH_ENABLED=1`` 强制开
    2. ``PYTEST_CURRENT_TEST`` 存在时 (pytest) 强制开 (测试 sandbox)
    3. 否则保持默认关闭

    Phase 2.2 增量:
    * ``AGENT_RUNTIME_INTERRUPT_V2_ENABLED=1`` 强制开 ``interrupt_v2_enabled``;
    * 测试环境 (``PYTEST_CURRENT_TEST`` 存在) 时也强制开(测试显式 opt-in,
      防止测试态默认 sentinel 路径绕开 interrupt 路径覆盖)。

    Phase 2.4 增量:
    * ``AGENT_RUNTIME_REPAIR_AGENT_ENABLED=1`` 强制开 ``repair_agent_enabled``;
    * 测试 sandbox 也强制开(同 prep / interrupt 模式),默认关。
    """
    enabled = os.environ.get("AGENT_RUNTIME_LANGGRAPH_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    interrupt_enabled_env = os.environ.get(
        "AGENT_RUNTIME_INTERRUPT_V2_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    prep_enabled_env = os.environ.get(
        "AGENT_RUNTIME_PREPARATION_AGENT_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    repair_enabled_env = os.environ.get(
        "AGENT_RUNTIME_REPAIR_AGENT_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    incremental_enabled_env = os.environ.get(
        "AGENT_RUNTIME_INCREMENTAL_AGENT_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    # Phase 2.8A: 生产调度总开关 — 与 langgraph_enabled 独立。
    # 测试 sandbox 自动开(让单测可走完整 dispatcher 路径);
    # 生产仅当 AGENT_RUNTIME_PRODUCTION_DISPATCH_ENABLED=1 时开启。
    prod_dispatch_env = os.environ.get(
        "AGENT_RUNTIME_PRODUCTION_DISPATCH_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    # Phase 2.8C:动态 Agent API 入口总开关 — 与 production_dispatch_enabled 独立。
    # 默认 False;env AGENT_RUNTIME_DYNAMIC_AGENT_API_ENABLED=1 显式 opt-in;
    # 测试 sandbox (PYTEST_CURRENT_TEST) auto-on。
    # 守禁令 #31:不允许 dynamic_agent_api 在 production 默认开启。
    dynamic_agent_api_env = os.environ.get(
        "AGENT_RUNTIME_DYNAMIC_AGENT_API_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    phase29b_narrative_env = os.environ.get(
        "AGENT_RUNTIME_PHASE29B_NARRATIVE_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    phase29b_preparation_narrative_env = os.environ.get(
        "AGENT_RUNTIME_PHASE29B_PREPARATION_NARRATIVE_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    phase29b_repair_narrative_env = os.environ.get(
        "AGENT_RUNTIME_PHASE29B_REPAIR_NARRATIVE_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    phase29b_incremental_narrative_env = os.environ.get(
        "AGENT_RUNTIME_PHASE29B_INCREMENTAL_NARRATIVE_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    # Phase 2.9B.4: LLM-first Tool / task-summary narrative. All opt-in default off.
    phase29b_tool_narrative_env = os.environ.get(
        "AGENT_RUNTIME_PHASE29B_TOOL_NARRATIVE_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    phase29b_tool_narrative_blocking_env = os.environ.get(
        "AGENT_RUNTIME_PHASE29B_TOOL_NARRATIVE_BLOCKING_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    phase29b_tool_narrative_final_only_env = os.environ.get(
        "AGENT_RUNTIME_PHASE29B_TOOL_NARRATIVE_FINAL_ONLY", "1"
    ).strip().lower() in {"1", "true", "yes"}
    phase29b_task_summary_narrative_env = os.environ.get(
        "AGENT_RUNTIME_PHASE29B_TASK_SUMMARY_NARRATIVE_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    phase29b_deterministic_fallback_env = os.environ.get(
        "AGENT_RUNTIME_PHASE29B_DETERMINISTIC_FALLBACK_ENABLED", "1"
    ).strip().lower() in {"1", "true", "yes"}
    phase29b_narrative_stream_persist_env = os.environ.get(
        "AGENT_RUNTIME_PHASE29B_NARRATIVE_STREAM_PERSIST_ENABLED", "1"
    ).strip().lower() in {"1", "true", "yes"}
    phase29b_narrative_timeout_env = os.environ.get(
        "AGENT_RUNTIME_PHASE29B_NARRATIVE_TIMEOUT_SECONDS", "45"
    )
    phase29b_narrative_repair_env = os.environ.get(
        "AGENT_RUNTIME_PHASE29B_NARRATIVE_REPAIR_ATTEMPTS", "1"
    )
    in_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))
    if not enabled and in_pytest:
        enabled = True
    # interrupt_v2:显式 env 或测试 sandbox 才开;生产默认关
    interrupt_enabled = interrupt_enabled_env or in_pytest
    # preparation_agent: 镜像 interrupt_v2 模式(Plan §5)
    prep_enabled = prep_enabled_env or in_pytest
    # repair_agent: 镜像 preparation 模式(Plan §4)
    repair_enabled = repair_enabled_env or in_pytest
    # incremental_agent: Phase 2.5;镜像 repair_agent 模式
    incremental_enabled = incremental_enabled_env or in_pytest
    # production_dispatch: 镜像 prep/repair 模式 — 显式 env 或测试 sandbox 才开
    production_dispatch_enabled = prod_dispatch_env or in_pytest
    # dynamic_agent_api: Phase 2.8C — 显式 env 或测试 sandbox 才开
    dynamic_agent_api_enabled = dynamic_agent_api_env or in_pytest
    return AgentRuntimeFeatureFlags(
        langgraph_enabled=enabled,
        interrupt_v2_enabled=interrupt_enabled,
        preparation_agent_enabled=prep_enabled,
        repair_agent_enabled=repair_enabled,
        incremental_agent_enabled=incremental_enabled,
        force_in_tests=in_pytest,
        production_dispatch_enabled=production_dispatch_enabled,
        dynamic_agent_api_enabled=dynamic_agent_api_enabled,
        phase29b_narrative_enabled=phase29b_narrative_env,
        phase29b_preparation_narrative_enabled=phase29b_preparation_narrative_env,
        phase29b_repair_narrative_enabled=phase29b_repair_narrative_env,
        phase29b_incremental_narrative_enabled=phase29b_incremental_narrative_env,
        phase29b_tool_narrative_enabled=phase29b_tool_narrative_env,
        phase29b_tool_narrative_blocking_enabled=phase29b_tool_narrative_blocking_env,
        phase29b_tool_narrative_final_only=phase29b_tool_narrative_final_only_env,
        phase29b_task_summary_narrative_enabled=phase29b_task_summary_narrative_env,
        phase29b_deterministic_fallback_enabled=phase29b_deterministic_fallback_env,
        phase29b_narrative_stream_persist_enabled=phase29b_narrative_stream_persist_env,
        phase29b_narrative_timeout_seconds=int(phase29b_narrative_timeout_env or 45),
        phase29b_narrative_repair_attempts=int(phase29b_narrative_repair_env or 1),
    )


def reset_feature_flags_for_test() -> None:
    """仅供测试使用 - Phase 2.0 范围内没有全局可变 flag,本函数为 no-op。

    保留 API 以便 Phase 2.1+ 引入可变 cache 时无需修改调用方。
    """
    return None
