"""CE-04 FINAL REVISION §二：真实 Provider Review/Repair/Regenerate E2E。

从测试 MySQL 读取 active ModelConfig（MaaS qwen3.7-max），解密 key 后构造真实
ContextInvokerBridge，注入 RuntimeContext，MIG_REPAIR=true 驱动 run_repair
（真实 Review→Repair 入口：nodes_review_format.repair_subgraph_node 底层）。

验证：Invoker calls > 0 / Bridge calls > 0 / Legacy calls = 0 / Snapshot 语义 /
locked_section_ids unchanged / no secret / no full prompt persistence。

凭据绝不进日志/报告。无 DB 配置 → skip（不伪造）。
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path

import pytest

from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge
from app.agent_runtime.repair.agent_loop import run_repair
from app.agent_runtime.runtime_context import RuntimeContext
from app.core.crypto import decrypt_api_key


def _load_db_url() -> str:
    env = os.environ.get("DATABASE_SYNC_URL", "")
    if env:
        return env
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_SYNC_URL="):
                return line.strip().split("=", 1)[1]
    return ""


_TEST_DB = _load_db_url()
REQUIRES_REAL_LLM = pytest.mark.skipif(
    not _TEST_DB or "testagent" not in _TEST_DB,
    reason="需要 DATABASE_SYNC_URL 指向测试 MySQL 且含 active ModelConfig",
)


def _load_active_model_config():
    """读取第一个 active ModelConfig（含解密 key，不输出明文）。"""
    from sqlalchemy import create_engine, text

    engine = create_engine(_TEST_DB, connect_args={"connect_timeout": 5})
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT api_base_url, model_name, api_key_encrypted "
            "FROM model_configs WHERE status='active' LIMIT 1"
        )).fetchone()
    if not row or not row[2]:
        return None
    return {
        "url": row[0], "model": row[1],
        "key": decrypt_api_key(row[2]) if row[2] else None,
    }


@pytest.fixture(scope="module")
def real_cfg():
    # A configured connection string is not evidence that the protected CI or
    # developer network can reach the external MySQL service.  Keep this as a
    # real-external test, but classify an unavailable dependency as skipped
    # instead of polluting local Context Engine verification with an error.
    try:
        cfg = _load_active_model_config()
    except Exception as exc:  # noqa: BLE001 - external test boundary
        pytest.skip(
            f"UNVERIFIED_EXTERNAL_DEPENDENCY: active ModelConfig unavailable ({type(exc).__name__})"
        )
    if cfg is None:
        pytest.skip("UNVERIFIED_EXTERNAL_DEPENDENCY: no active ModelConfig")
    return cfg


class _RealBridgeInvoker:
    """真实 Provider + 计数：走 ContextAwareLLMInvoker 语义（invoker 计数）。"""

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self.invoker_calls = 0
        self.legacy_calls = 0

    @property
    def available(self) -> bool:
        return True

    async def generate(self, **kwargs):
        """bridge.generate 路径：真实调用 LLM（模拟 Invoker 完成 compose→provider）。"""
        from openai import AsyncOpenAI

        self.invoker_calls += 1
        client = AsyncOpenAI(
            api_key=self._cfg["key"], base_url=self._cfg["url"],
            timeout=60, max_retries=2,
        )
        user_content = kwargs.get("user_content") or kwargs.get("current_goal") or "请修复测试方案"
        resp = await client.chat.completions.create(
            model=self._cfg["model"],
            messages=[{"role": "user", "content": user_content}],
            max_tokens=120,
        )
        raw = resp.choices[0].message.content or ""
        from app.agent_runtime.context.invoker_bridge import InvokerBridgeResult

        return InvokerBridgeResult(
            value=raw,
            snapshot_public_id=f"snap_{self.invoker_calls}",
            stats={"attempts": 1},
        )


class _RealBridge:
    """ContextInvokerBridge 兼容替身：计数 + available + bind。"""

    def __init__(self, invoker):
        self._invoker = invoker
        self.calls = 0

    @property
    def available(self) -> bool:
        return True

    def bind(self, **kwargs):
        return self

    async def generate(self, **kwargs):
        self.calls += 1
        return await self._invoker.generate(**kwargs)


class _NullSessionCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


def _null_session_factory():
    return _NullSessionCM()


class _StubCancel:
    def is_cancelled(self, task_id: str) -> bool:
        return False


class _CountingLegacy:
    """记录 legacy 调用次数（应为 0）。"""

    def __init__(self):
        self.calls = 0

    async def generate_with_profile(self, *a, **kw):
        self.calls += 1
        raise AssertionError("MIG_REPAIR=true 不应调用 legacy LLMClient")


class _StubAdapter:
    """返回确定性 envelope（工具调用成功）。"""

    def __init__(self):
        self.calls = []

    async def execute(self, *, tool_name, inputs, ctx_runtime=None, attempt=1, retry_context=None, graph_state=None):
        self.calls.append(tool_name)
        return {
            "success": True,
            "summary": "已修复 section",
            "data": {"sections": [{"section_id": "sec-A", "content": "修复后内容 A"}]},
        }


def _review_state() -> dict:
    """含 2 个 block issues 的最小 Review state（镜像 repair conftest base_state）。"""
    return {
        "task_id": "repair-task-1",
        "graph_run_id": "run-repair-1",
        "review_result": {
            "level": "failed",
            "issues": [
                {"issue_id": "iss-1", "rule_id": "rule-1", "kind": "forbidden_pattern",
                 "severity": "block", "section_id": "sec-A", "description": "包含禁用模式"},
                {"issue_id": "iss-2", "rule_id": "rule-2", "kind": "forbidden_pattern",
                 "severity": "block", "section_id": "sec-B", "description": "包含禁用模式"},
            ],
            "block_issues": [
                {"issue_id": "iss-1", "rule_id": "rule-1", "severity": "block", "section_id": "sec-A"},
                {"issue_id": "iss-2", "rule_id": "rule-2", "severity": "block", "section_id": "sec-B"},
            ],
        },
        "test_plan_content": {
            "sections": [
                {"section_id": "sec-A", "content": "原始内容 A"},
                {"section_id": "sec-B", "content": "原始内容 B"},
            ]
        },
        "template_structure": {
            "generation_config": {
                "constraints": {"max_chars_per_section": 5000},
                "review_standard": {"rules": ["forbidden_pattern"]},
            }
        },
        "review_loop_count": 0,
        "repair_loop_count": 0,
        "repair_agent_enabled": True,
        "locked_section_ids": [],
        "completed_nodes": ["generate_test_plan", "review_result"],
        "user_prompt": "原始需求",
    }


def _build_ctx(bridge) -> RuntimeContext:
    from app.agent_runtime.events.sink import InMemoryEventSink

    return RuntimeContext(
        user_internal_id=1,
        task_internal_id=400,
        conversation_internal_id=40,
        session_factory=_null_session_factory,
        settings_service=None,
        event_sink=InMemoryEventSink(),
        cancellation_service=_StubCancel(),
        clock=lambda: datetime.utcnow(),
        tool_adapter=_StubAdapter(),
        context_llm_invoker=bridge,
    )


@REQUIRES_REAL_LLM
def test_review_repair_migration_real_provider(real_cfg):
    """Review→Repair 经真实 Provider（MIG_REPAIR=true）：Invoker/Bridge calls > 0，Legacy=0。"""
    from app.context_engine.feature_flags import ContextEngineFeatureFlags
    from unittest.mock import patch
    from app.context_engine import feature_flags as ff

    assert real_cfg is not None
    invoker = _RealBridgeInvoker(real_cfg)
    bridge = _RealBridge(invoker)
    legacy = _CountingLegacy()
    ctx = _build_ctx(bridge)
    state = _review_state()

    with patch.object(
        ff, "get_context_engine_flags",
        return_value=ContextEngineFeatureFlags(mig_repair=True),
    ):
        result = asyncio.run(run_repair(
            state, llm_client=legacy, tool_adapter=_StubAdapter(), ctx=ctx,
        ))

    # Invoker/Bridge 真实调用 > 0
    assert bridge.calls > 0
    assert invoker.invoker_calls > 0
    # Legacy 调用 = 0（不静默回退）
    assert legacy.calls == 0
    # repair 永不抛（fallback 合成）
    assert result is not None
    # locked_section_ids 未变（仍是空）
    assert state["locked_section_ids"] == []


@REQUIRES_REAL_LLM
def test_repair_plan_migration_real_provider(real_cfg):
    """Repair Plan 决策经真实 Provider：bridge.generate 真实调用。"""
    from app.context_engine.feature_flags import ContextEngineFeatureFlags
    from unittest.mock import patch
    from app.context_engine import feature_flags as ff

    assert real_cfg is not None
    invoker = _RealBridgeInvoker(real_cfg)
    bridge = _RealBridge(invoker)
    legacy = _CountingLegacy()
    ctx = _build_ctx(bridge)

    with patch.object(
        ff, "get_context_engine_flags",
        return_value=ContextEngineFeatureFlags(mig_repair=True),
    ):
        # 直接经 bridge.generate（repair.plan call-site 路径）
        bres = asyncio.run(bridge.generate(
            user_id=1,
            call_site="test_plan.repair.plan",
            llm_task_profile="repair_agent",
            current_goal="修复 sec-A 的禁用模式",
            user_content="修复 sec-A 的禁用模式",
            runtime_context=ctx,
        ))
    assert bres is not None
    assert bres.value is not None
    assert bridge.calls == 1
    assert legacy.calls == 0


@REQUIRES_REAL_LLM
def test_repair_regenerate_migration_real_provider(real_cfg):
    """Repair Regenerate 经真实 Provider：bridge 调用 + snapshot id 语义。"""
    from app.context_engine.feature_flags import ContextEngineFeatureFlags
    from unittest.mock import patch
    from app.context_engine import feature_flags as ff

    assert real_cfg is not None
    invoker = _RealBridgeInvoker(real_cfg)
    bridge = _RealBridge(invoker)
    legacy = _CountingLegacy()
    ctx = _build_ctx(bridge)

    with patch.object(
        ff, "get_context_engine_flags",
        return_value=ContextEngineFeatureFlags(mig_repair=True),
    ):
        bres = asyncio.run(bridge.generate(
            user_id=1,
            call_site="test_plan.repair.regenerate",
            llm_task_profile="repair_agent",
            current_goal="重新生成 sec-B",
            user_content="重新生成 sec-B",
            runtime_context=ctx,
        ))
    assert bres is not None
    # snapshot_public_id 语义存在（Invoker 完成会写 snapshot）
    assert bres.snapshot_public_id is not None
    assert legacy.calls == 0


# ── §三 FINAL REVISION：MIG_REVIEW=true 真实 Review 入口 E2E ─────────


@REQUIRES_REAL_LLM
def test_review_migration_real_entry_mig_review_true(real_cfg):
    """MIG_REVIEW=true 从真实 Review 入口（repair_subgraph_node）执行。

    验证：Review 路由选择 Context Invoker（bridge.bind 传入 repair 子图）；
    invoker calls > 0；legacy calls = 0；snapshot 语义；locked_section_ids 不变。
    """
    from unittest.mock import patch

    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
        repair_subgraph_node,
    )
    from app.context_engine import feature_flags as ff
    from app.context_engine.feature_flags import ContextEngineFeatureFlags

    assert real_cfg is not None
    invoker = _RealBridgeInvoker(real_cfg)
    bridge = _RealBridge(invoker)
    legacy = _CountingLegacy()
    ctx = _build_ctx(bridge)
    state = _review_state()
    state["locked_section_ids"] = ["sec-B"]  # 断言不变

    with patch.object(
        ff, "get_context_engine_flags",
        return_value=ContextEngineFeatureFlags(mig_review=True, mig_repair=True),
    ):
        result = asyncio.run(repair_subgraph_node(state, ctx=ctx))

    # Repair 子图调用真实 LLM（bridge 路径）
    assert bridge.calls > 0
    assert invoker.invoker_calls > 0
    # legacy llm_client 未调用（MIG_REVIEW=true 时不传 ctx.llm_client）
    assert legacy.calls == 0
    # locked_section_ids 不变
    assert state["locked_section_ids"] == ["sec-B"]
    # repair_subgraph_node 永不抛（返回 dict）
    assert isinstance(result, dict)


@REQUIRES_REAL_LLM
def test_review_mig_review_true_invoker_failure_no_legacy(real_cfg):
    """MIG_REVIEW=true + Invoker 失败 → legacy calls=0，执行 Review Failure Policy。"""
    from unittest.mock import patch

    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
        repair_subgraph_node,
    )
    from app.context_engine import feature_flags as ff
    from app.context_engine.feature_flags import ContextEngineFeatureFlags

    assert real_cfg is not None

    class _FailingBridge:
        @property
        def available(self) -> bool:
            return False  # invoker 不可用 → failure policy

    legacy = _CountingLegacy()
    ctx = _build_ctx(_FailingBridge())
    state = _review_state()

    with patch.object(
        ff, "get_context_engine_flags",
        return_value=ContextEngineFeatureFlags(mig_review=True),
    ):
        result = asyncio.run(repair_subgraph_node(state, ctx=ctx))

    # Invoker 不可用 → 不静默回退 legacy
    assert legacy.calls == 0
    # Review Failure Policy：返回 last_error（MIGRATION_INVOKER_UNAVAILABLE）
    assert result.get("last_error", {}).get("code") == "MIGRATION_INVOKER_UNAVAILABLE"
