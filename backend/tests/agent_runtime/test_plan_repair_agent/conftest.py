"""Phase 2.4 Review Repair Agent 测试 fixtures.

镜像 ``test_plan_prep_agent/conftest.py`` 但适配 3 个工具:

* ``ResultReviewTool``    — 复审
* ``TestPlanRegenTool``   — 重写 section
* ``KnowledgeSearchTool`` — 知识库检索

提供:
* ``FakeLLMClient``     — scripted JSON 响应栈
* ``StubToolAdapter``   — 三个工具的 envelope 注入(顺序执行栈)
* ``runtime_ctx``       — RuntimeContext with tool_adapter + event_sink
* ``base_state``        — 含 ``review_result`` / ``locked_section_ids`` 的最小 state
"""

from __future__ import annotations

import json as _json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import pytest

from app.agent_runtime.runtime_context import RuntimeContext


# ── Fake LLM client ───────────────────────────────────────────────────────


class FakeLLMClient:
    """镜像 prep conftest 的 FakeLLMClient;profile name 改 ``repair_agent``。"""

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
        self, profile, content, *, parser=None, system_prompt_override=None
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
        try:
            parsed = _json.loads(raw)
        except Exception:
            parsed = raw
        return _FakeResult(success=True, parsed=parsed, error_message=None)


class _FakeResult:
    __slots__ = (
        "task_name",
        "raw_text",
        "parsed",
        "success",
        "error_type",
        "error_message",
    )

    def __init__(
        self, *, success: bool, parsed: Any, error_message: Optional[str]
    ) -> None:
        self.task_name = "repair_agent"
        self.raw_text = "" if not parsed else str(parsed)[:200]
        self.parsed = parsed
        self.success = success
        self.error_type = None if success else "llm_error"
        self.error_message = error_message


@pytest.fixture
def fake_llm():
    return FakeLLMClient()


# ── Stub tool adapter (3 工具) ────────────────────────────────────────────


class StubToolAdapter:
    """3 工具 envelope 注入栈。

    * ``envelopes_per_tool`` — dict[tool_name] -> List[envelope];按调用顺序弹
    * ``default_envelopes``  — dict[tool_name] -> envelope;兜底
    * 每次 ``execute`` 在 ``self.calls`` 留一条记录
    """

    def __init__(
        self,
        envelopes_per_tool: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        default_envelopes: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self.envelopes_per_tool: Dict[str, List[Dict[str, Any]]] = dict(
            envelopes_per_tool or {}
        )
        self.default_envelopes: Dict[str, Dict[str, Any]] = dict(
            default_envelopes or {}
        )
        self.calls: List[Dict[str, Any]] = []
        self._counters: Dict[str, int] = {}

    async def execute(
        self, *, tool_name, inputs, ctx_runtime, attempt=1, retry_context=None,
        graph_state=None,
    ):
        self.calls.append(
            {
                "tool_name": tool_name,
                "inputs": dict(inputs or {}),
                "attempt": attempt,
                # Phase 2.9A.X: agent_loop 现在传 graph_state,
                # 记录下来便于测试断言「业务字段已透传给工具」。
                "graph_state_keys": sorted((graph_state or {}).keys()),
            }
        )
        envs = self.envelopes_per_tool.get(tool_name) or []
        i = self._counters.get(tool_name, 0)
        if i < len(envs):
            self._counters[tool_name] = i + 1
            return dict(envs[i])
        if tool_name in self.default_envelopes:
            return dict(self.default_envelopes[tool_name])
        # Fallback generic envelope
        return {
            "success": True,
            "tool_name": tool_name,
            "task_id": "stub",
            "data": {},
            "summary": "",
            "warnings": [],
            "error": None,
            "duration_ms": 1,
            "attempt": 1,
        }


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
    def __init__(self, cancel: bool = False) -> None:
        self._cancel = cancel

    def set_cancel(self, v: bool) -> None:
        self._cancel = v

    def is_cancelled(self, task_id: str) -> bool:
        return self._cancel


@pytest.fixture
def in_memory_sink():
    from app.agent_runtime.events.sink import InMemoryEventSink
    return InMemoryEventSink()


@pytest.fixture
def runtime_ctx(in_memory_sink, stub_adapter):
    """RuntimeContext 含 tool_adapter + event_sink。"""
    return RuntimeContext(
        user_internal_id=1,
        task_internal_id=400,
        conversation_internal_id=40,
        session_factory=_null_session_factory,
        settings_service=None,
        event_sink=in_memory_sink,
        cancellation_service=_StubCancel(),
        clock=lambda: datetime.utcnow(),
        tool_adapter=stub_adapter,
    )


# ── Base state ────────────────────────────────────────────────────────────


def _make_review_issues(
    issue_ids: List[str],
    section_ids: List[str],
    kind: str = "forbidden_pattern",
    severity: str = "block",
    *,
    repairable: bool = True,
) -> List[Dict[str, Any]]:
    """标准化 ReviewIssue dict 工厂(Phase 2.1 bug-fix 后字段集)。"""
    return [
        {
            "issue_id": iid,
            "rule_id": f"rule-{iid}",
            "kind": kind,
            "severity": severity,
            "section_id": sid,
            "field_path": f"section.{sid}.content",
            "message": f"{kind} violation in {sid}",
            "evidence": f"…{kind}…",
            "expected_rule": f"must not contain {kind}",
            "repairable": repairable,
            "suggested_strategy": "regenerate_section",
        }
        for iid, sid in zip(issue_ids, section_ids)
    ]


def _make_review_result(
    issue_ids: List[str],
    section_ids: List[str],
    *,
    level: str = "failed",
    kind: str = "forbidden_pattern",
    severity: str = "block",
) -> Dict[str, Any]:
    """review_result 含双字段:``review_issues``(新) + ``block_issues``(旧)。"""
    issues = _make_review_issues(issue_ids, section_ids, kind=kind, severity=severity)
    if severity == "block":
        block = [i for i in issues if i["severity"] == "block"]
    else:
        block = []
    return {
        "level": level,
        "review_issues": issues,
        "block_issues": block,
        "rule_issues": issues,  # 镜像 ResultReviewTool 旧字段
        "summary": f"{len(issues)} issues",
    }


@pytest.fixture
def base_state():
    """含 2 个 block issues 的最小 state。"""
    return {
        "task_id": "repair-task-1",
        "graph_run_id": "run-repair-1",
        "review_result": _make_review_result(
            ["iss-1", "iss-2"], ["sec-A", "sec-B"], level="failed"
        ),
        "test_plan_content": {
            "sections": [
                {"section_id": "sec-A", "content": "原始内容 A"},
                {"section_id": "sec-B", "content": "原始内容 B"},
                {"section_id": "sec-C", "content": "原始内容 C"},
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


# ── clock ─────────────────────────────────────────────────────────────────


@pytest.fixture
def make_clock():
    """monotonic clock factory + advance helper."""
    _t = [0.0]

    def factory():
        return _t[0]

    def advance(seconds: float) -> None:
        _t[0] += seconds

    return factory, advance


# ── Scripted JSON helpers ─────────────────────────────────────────────────


def call_regen_decision(
    target_section_ids: List[str],
    target_issue_ids: List[str],
    *,
    summary: str = "修复 section",
    iteration: Optional[int] = None,
) -> str:
    issues_list: List[Dict[str, Any]] = []
    for iid, sid in zip(target_issue_ids, target_section_ids):
        item: Dict[str, Any] = {"issue_id": iid, "section_id": sid}
        if iteration is not None:
            item["iteration"] = iteration
        issues_list.append(item)
    return _json.dumps(
        {
            "action": "call_tool",
            "tool_name": "TestPlanRegenTool",
            "tool_arguments": {
                "section_ids": list(target_section_ids),
                "issues": issues_list,
                "generation_config_subset": {
                    "constraints": {"max_chars_per_section": 5000}
                },
            },
            "target_issue_ids": list(target_issue_ids),
            "target_section_ids": list(target_section_ids),
            "suggested_strategy": "regenerate_section",
            "decision_summary": summary,
            "public_update": f"正在修复 {len(target_section_ids)} 个 section",
            "expected_result": "issues 全部解决",
            "confidence": 0.8,
        },
        ensure_ascii=False,
    )


def call_review_decision(*, summary: str = "复审") -> str:
    return _json.dumps(
        {
            "action": "call_tool",
            "tool_name": "ResultReviewTool",
            "tool_arguments": {},
            "target_issue_ids": [],
            "target_section_ids": [],
            "suggested_strategy": "re_review",
            "decision_summary": summary,
            "public_update": "复审中…",
            "expected_result": "确认无 block issue",
            "confidence": 0.7,
        },
        ensure_ascii=False,
    )


def call_kb_decision(query: str, *, summary: str = "KB 检索") -> str:
    return _json.dumps(
        {
            "action": "call_tool",
            "tool_name": "KnowledgeSearchTool",
            "tool_arguments": {"query": query, "top_k": 3, "labels": None},
            "target_issue_ids": [],
            "target_section_ids": [],
            "suggested_strategy": "kb_lookup",
            "decision_summary": summary,
            "public_update": f"检索:{query}",
            "expected_result": "≥1 chunk",
            "confidence": 0.6,
        },
        ensure_ascii=False,
    )


def finish_decision(
    target_issue_ids: Optional[List[str]] = None,
    *,
    summary: str = "修复完成",
    confidence: float = 0.9,
) -> str:
    return _json.dumps(
        {
            "action": "finish",
            "tool_name": None,
            "tool_arguments": None,
            "target_issue_ids": list(target_issue_ids or []),
            "target_section_ids": [],
            "suggested_strategy": None,
            "decision_summary": summary,
            "public_update": "修复阶段完成。",
            "expected_result": "review_passed=True",
            "confidence": confidence,
        },
        ensure_ascii=False,
    )


def fail_decision(reason: str = "修复失败") -> str:
    return _json.dumps(
        {
            "action": "fail",
            "tool_name": None,
            "tool_arguments": None,
            "target_issue_ids": [],
            "target_section_ids": [],
            "suggested_strategy": None,
            "decision_summary": reason,
            "public_update": "修复阶段无法继续。",
            "expected_result": None,
            "confidence": 0.0,
        },
        ensure_ascii=False,
    )


# ── Envelope helpers ──────────────────────────────────────────────────────


def regen_envelope_ok(section_ids: List[str], *, regen_count: int = 1) -> Dict[str, Any]:
    return {
        "success": True,
        "tool_name": "TestPlanRegenTool",
        "task_id": "stub",
        "data": {
            "regenerated_section_ids": list(section_ids),
            "regen_count": regen_count,
            "issues_resolved": regen_count,
        },
        "summary": "重写完成",
        "warnings": [],
        "error": None,
        "duration_ms": 5,
        "attempt": 1,
    }


def review_envelope_passed() -> Dict[str, Any]:
    return {
        "success": True,
        "tool_name": "ResultReviewTool",
        "task_id": "stub",
        "data": {
            "level": "passed",
            "review_issues": [],
            "block_issues": [],
            "summary": "复审通过",
        },
        "summary": "复审通过",
        "warnings": [],
        "error": None,
        "duration_ms": 4,
        "attempt": 1,
    }


def review_envelope_failed(
    issue_ids: List[str],
    section_ids: List[str],
    *,
    kind: str = "forbidden_pattern",
) -> Dict[str, Any]:
    issues = _make_review_issues(issue_ids, section_ids, kind=kind, severity="block")
    return {
        "success": True,
        "tool_name": "ResultReviewTool",
        "task_id": "stub",
        "data": {
            "level": "failed",
            "review_issues": issues,
            "block_issues": [i for i in issues if i["severity"] == "block"],
            "summary": f"复审仍发现 {len(issues)} 个 issue",
        },
        "summary": "复审仍发现 block issue",
        "warnings": [],
        "error": None,
        "duration_ms": 4,
        "attempt": 1,
    }


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


def tool_envelope_failure(
    tool_name: str, code: str = "TOOL_TIMEOUT", recoverable: bool = True,
) -> Dict[str, Any]:
    return {
        "success": False,
        "tool_name": tool_name,
        "task_id": "stub",
        "data": {},
        "summary": "",
        "warnings": [],
        "error": {"code": code, "message": f"{tool_name} failed", "recoverable": recoverable},
        "duration_ms": 1,
        "attempt": 1,
    }