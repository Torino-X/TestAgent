"""Phase 2.3 Preparation Agent 测试 fixtures.

提供:
* ``FakeLLMClient``       — scripted JSON 响应栈;模拟 generate_with_profile
* ``scripted_response``   — 上下文管理器,简化 LLM 注入
* ``StubToolAdapter``     — KnowledgeSearchTool envelope 注入(替代真实 adapter)
* ``runtime_ctx``         — RuntimeContext with tool_adapter + event_sink
* ``base_state``          — minimal TestPlanGraphState 切片(供 agent_loop)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import pytest

from app.agent_runtime.runtime_context import RuntimeContext


# ── Fake LLM client ───────────────────────────────────────────────────────


class FakeLLMClient:
    """模拟 LLMClient.generate_with_profile。

    每次调用从 ``self.responses`` 队列 pop 一条 JSON 字符串,封装成
    LLMProfileResult-like 对象(只暴露 .parsed / .success / .error_message)。

    若 ``fail_next`` 设 True,下一次调用返回 success=False(模拟 LLM 调用失败)。
    """

    def __init__(self, responses: Optional[List[str]] = None) -> None:
        self.responses: List[str] = list(responses or [])
        self.calls: List[Dict[str, Any]] = []
        self.fail_next: bool = False
        self.fail_with: Optional[Exception] = None

    def push(self, json_str: str) -> None:
        self.responses.append(json_str)

    def extend(self, json_strs: List[str]) -> None:
        self.responses.extend(json_strs)

    async def generate_with_profile(
        self,
        profile,
        content,
        *,
        parser=None,
        system_prompt_override=None,
    ):
        self.calls.append(
            {
                "profile": getattr(profile, "name", None),
                "content_len": len(content or ""),
                # Phase 2.9B.2: 记录 override,证明动态 Prompt 到达调用点。
                "system_prompt_override": system_prompt_override,
            }
        )
        if self.fail_next:
            self.fail_next = False
            err = self.fail_with
            self.fail_with = None
            return _FakeResult(
                success=False,
                parsed=None,
                error_message=str(err) if err else "fake_llm_error",
            )
        if not self.responses:
            raise RuntimeError("FakeLLMClient: no scripted responses left")
        raw = self.responses.pop(0)
        # Return parsed as dict (preferred) — agent_loop will json.dumps + AgentDecision.model_validate_json
        import json as _json
        try:
            parsed = _json.loads(raw)
        except Exception:
            parsed = raw
        return _FakeResult(success=True, parsed=parsed, error_message=None)


class _FakeResult:
    __slots__ = ("task_name", "raw_text", "parsed", "success", "error_type", "error_message")

    def __init__(self, *, success: bool, parsed: Any, error_message: Optional[str]) -> None:
        self.task_name = "preparation_agent"
        self.raw_text = "" if not parsed else (str(parsed)[:200])
        self.parsed = parsed
        self.success = success
        self.error_type = None if success else "llm_error"
        self.error_message = error_message


# ── Scripted response helper ──────────────────────────────────────────────


@pytest.fixture
def fake_llm():
    return FakeLLMClient()


# ── Stub tool adapter (替代 TestAgentToolAdapter) ───────────────────────


class StubToolAdapter:
    """模拟 TestAgentToolAdapter.execute。

    调用顺序按 self.envelopes_kb 弹出;若该 tool_name 不在 envelopes_kb
    则按 default_envelope 返回。

    调用记录在 self.calls (list of {tool_name, inputs})。
    """

    def __init__(
        self,
        envelopes_kb: Optional[List[Dict[str, Any]]] = None,
        default_envelope: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.envelopes_kb: List[Dict[str, Any]] = list(envelopes_kb or [])
        self.default_envelope: Dict[str, Any] = default_envelope or {
            "success": True,
            "tool_name": "KnowledgeSearchTool",
            "task_id": "stub",
            "data": {},
            "summary": "",
            "warnings": [],
            "error": None,
            "duration_ms": 1,
            "attempt": 1,
        }
        self.calls: List[Dict[str, Any]] = []
        self.kb_index = 0

    async def execute(self, *, tool_name, inputs, ctx_runtime, attempt=1, retry_context=None):
        self.calls.append({"tool_name": tool_name, "inputs": dict(inputs)})
        if tool_name == "KnowledgeSearchTool":
            if self.kb_index < len(self.envelopes_kb):
                env = self.envelopes_kb[self.kb_index]
                self.kb_index += 1
                return dict(env)
        return dict(self.default_envelope)


@pytest.fixture
def stub_adapter():
    return StubToolAdapter()


# ── Event sink + runtime context ──────────────────────────────────────────


class _NullSessionCM:
    async def __aenter__(self_inner):
        return None

    async def __aexit__(self_inner, *args):
        return False


def _null_session_factory():
    return _NullSessionCM()


class _StubCancel:
    def is_cancelled(self, task_id: str) -> bool:  # pragma: no cover
        return False


@pytest.fixture
def in_memory_sink():
    from app.agent_runtime.events.sink import InMemoryEventSink
    return InMemoryEventSink()


@pytest.fixture
def runtime_ctx(in_memory_sink, stub_adapter):
    """RuntimeContext 含 tool_adapter + event_sink — 供 agent_loop 直接用。"""
    return RuntimeContext(
        user_internal_id=1,
        task_internal_id=200,
        conversation_internal_id=20,
        session_factory=_null_session_factory,
        settings_service=None,
        event_sink=in_memory_sink,
        cancellation_service=_StubCancel(),
        clock=lambda: datetime.utcnow(),
        tool_adapter=stub_adapter,
    )


# ── Base state helpers ────────────────────────────────────────────────────


@pytest.fixture
def base_state():
    """最小可工作 state 切片,供 run_preparation 读取。"""
    return {
        "user_prompt": "请生成单元测试方案,目标模块:用户认证服务",
        "requirement_summary": "实现用户登录/注册接口,要求覆盖异常场景",
        "template_summary": "标准测试方案模板(测试概述 + 测试项 + 风险 + 准入)",
        "knowledge_search_result": None,
        "kb_skip_reason": None,
        "completed_nodes": [],
    }


@pytest.fixture
def make_clock():
    """Return a clock factory + setter for monotonic time injection."""
    _t = [0.0]

    def factory():
        return _t[0]

    def advance(seconds: float) -> None:
        _t[0] += seconds

    return factory, advance


# ── Common scripted JSON shortcuts ────────────────────────────────────────
#
# Phase 2.9B.3: 所有 scripted decision 都包含 action_reason(镜像生产 Prompt
# 的字段合同),避免测试假阳性(旧 fixture 无 action_reason → 永不触发
# extra_forbidden 校验路径)。


def _full_decision_update(headline: str, summary: str, impact: str, next_action: str) -> dict:
    """完整 decision_update 对象,满足 Phase 2.9B.3 五字段合同。"""
    return {
        "headline": headline,
        "summary": summary,
        "impact": impact,
        "next_action": next_action,
        "details": ["事实: 已确认"],
    }


def finish_decision(summary: str = "信息充足", confidence: float = 0.9) -> str:
    import json as _json
    return _json.dumps(
        {
            "action": "finish",
            "tool_name": None,
            "tool_arguments": None,
            "action_reason": "信息充足,进入下一步。",
            "decision_summary": summary,
            "public_update": "准备阶段完成,信息充足。",
            "decision_update": _full_decision_update(
                "准备阶段完成",
                "已评估需求、模板与知识库信息。",
                "信息充足,可直接进入章节确认。",
                "进入章节处理策略确认。",
            ),
            "expected_result": None,
            "confidence": confidence,
        },
        ensure_ascii=False,
    )


def call_kb_decision(query: str, *, summary: str = "调 KB 检索") -> str:
    import json as _json
    return _json.dumps(
        {
            "action": "call_tool",
            "tool_name": "KnowledgeSearchTool",
            "tool_arguments": {"query": query, "top_k": 3, "labels": None},
            "action_reason": "需要检索知识库确认业务规则。",
            "decision_summary": summary,
            "public_update": f"正在检索:{query}",
            "decision_update": _full_decision_update(
                "正在检索知识库",
                "模板中的字段需要知识库确认。",
                "检索结果将决定章节处理策略。",
                f"调用 KnowledgeSearchTool 检索:{query}。",
            ),
            "expected_result": "应返回 ≥1 chunk",
            "confidence": 0.7,
        },
        ensure_ascii=False,
    )


def ask_user_decision(field: str = "requirement_gap", question: str = "需要补充业务背景") -> str:
    import json as _json
    return _json.dumps(
        {
            "action": "ask_user",
            "tool_name": None,
            "tool_arguments": None,
            "action_reason": "发现需求缺口需要用户补充。",
            "decision_summary": f"发现缺口:{field}",
            "public_update": question,
            "clarification_gaps": [
                {
                    "field": field,
                    "description": question,
                    "severity": "medium",
                }
            ],
            "decision_update": _full_decision_update(
                "需要补充信息",
                "发现需求缺口,需要用户澄清。",
                "补充后将能继续生成测试方案。",
                "等待用户补充业务背景。",
            ),
            "expected_result": "应触发用户追问",
            "confidence": 0.6,
        },
        ensure_ascii=False,
    )


def fail_decision(reason: str = "决策明确失败") -> str:
    import json as _json
    return _json.dumps(
        {
            "action": "fail",
            "tool_name": None,
            "tool_arguments": None,
            "action_reason": "决策明确失败。",
            "decision_summary": reason,
            "public_update": "准备阶段无法继续。",
            "expected_result": None,
            "confidence": 0.0,
        },
        ensure_ascii=False,
    )


def kb_envelope_ok(chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "success": True,
        "tool_name": "KnowledgeSearchTool",
        "task_id": "stub",
        "data": {"chunks": chunks},
        "summary": "KB 检索完成",
        "warnings": [],
        "error": None,
        "duration_ms": 5,
        "attempt": 1,
    }


def kb_envelope_disabled(reason: str = "knowledge_disabled") -> Dict[str, Any]:
    return {
        "success": True,
        "tool_name": "KnowledgeSearchTool",
        "task_id": "stub",
        "data": {"disabled_by_config": True, "skip_reason": reason},
        "summary": "KB 未配置",
        "warnings": [],
        "error": None,
        "duration_ms": 1,
        "attempt": 1,
    }


def kb_envelope_recoverable_failure(message: str = "上游 KB 服务暂时不可用") -> Dict[str, Any]:
    return {
        "success": False,
        "tool_name": "KnowledgeSearchTool",
        "task_id": "stub",
        "data": {},
        "summary": "",
        "warnings": [],
        "error": {"code": "KB_TIMEOUT", "message": message, "recoverable": True},
        "duration_ms": 1,
        "attempt": 1,
    }
