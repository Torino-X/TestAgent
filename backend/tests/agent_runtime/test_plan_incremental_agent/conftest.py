"""Test fixtures for incremental agent tests (Phase 2.5).

镜像 preparation/conftest.py 与 repair/conftest.py 的设计,但 incremental 路径独立:

* ``FakeLLMClient``:脚本化返回 JSON;
* ``StubToolAdapter``:返回脚本化 envelope,不调真实 Tool;
* ``build_intent``:构造 IncrementalIntent;
* ``build_state``:构造 TestPlanGraphState (incremental_agent_enabled=True)。

Note:incremental 测试不需要 fixture 编排 LangGraph 编译图,直接调
``run_incremental`` 主循环 / ``filter_incremental_decision`` / ``build_incremental_prompt``
即可(graph compile 仅在 ``test_incremental_subgraph_compile`` 中真跑)。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import pytest

from app.agent_runtime.incremental.schemas import (
    ExistingArtifactRef,
    IncrementalIntent,
    ModificationScope,
)
from app.agent_runtime.graphs.test_plan.state import (
    TestPlanGraphState,
    make_empty_state,
)


# ── FakeLLMClient ───────────────────────────────────────────────


class FakeLLMClient:
    """脚本化 LLM 客户端;每次 ``generate_with_profile`` 返回下条 JSON。

    Phase 2.5 不真跑 LLM;响应列表耗尽时抛 ``IndexError``,主循环捕获
    后转 fallback。
    """

    def __init__(self, responses: List[Any]):
        self._responses = list(responses)
        self._idx = 0
        self.calls: List[Dict[str, Any]] = []

    async def generate_with_profile(
        self,
        profile,
        content,
        *,
        system_prompt_override: str = "",
        **_kw,
    ) -> Any:
        self.calls.append({
            "profile": getattr(profile, "name", "<unknown>"),
            "content_len": len(content or ""),
            "system_prompt_override_len": len(system_prompt_override or ""),
        })
        if self._idx >= len(self._responses):
            raise IndexError(
                f"FakeLLMClient exhausted at call {self._idx + 1}; "
                f"need more responses"
            )
        resp = self._responses[self._idx]
        self._idx += 1
        return resp


# ── StubToolAdapter ─────────────────────────────────────────────


class StubToolAdapter:
    """脚本化 tool 适配器,按 tool_name → envelope list 取。"""

    def __init__(self, envelopes: Dict[str, List[Dict[str, Any]]] | None = None):
        self._envelopes: Dict[str, List[Dict[str, Any]]] = envelopes or {}
        self._idx: Dict[str, int] = {}
        self.calls: List[Dict[str, Any]] = []

    async def execute(
        self,
        *,
        tool_name: str,
        inputs: Dict[str, Any],
        ctx_runtime,
        graph_state=None,
        **_kw,
    ) -> Dict[str, Any]:
        self.calls.append({
            "tool_name": tool_name,
            "inputs": inputs,
            "graph_state_present": graph_state is not None,
        })
        env_list = self._envelopes.get(tool_name, [])
        idx = self._idx.get(tool_name, 0)
        if idx >= len(env_list):
            return {
                "success": False,
                "tool_name": tool_name,
                "task_id": "test-task",
                "data": {},
                "summary": "StubToolAdapter exhausted",
                "warnings": [],
                "error": "exhausted",
                "duration_ms": 0,
                "attempt": 1,
            }
        env = env_list[idx]
        self._idx[tool_name] = idx + 1
        return env


def regen_envelope_ok(section_ids: List[str]) -> Dict[str, Any]:
    return {
        "success": True,
        "tool_name": "TestPlanRegenTool",
        "task_id": "test-task",
        "data": {"modified_section_ids": section_ids, "regen_ok": True},
        "summary": f"Regenerated {len(section_ids)} sections",
        "warnings": [],
        "error": None,
        "duration_ms": 100,
        "attempt": 1,
    }


def review_envelope_passed() -> Dict[str, Any]:
    return {
        "success": True,
        "tool_name": "ResultReviewTool",
        "task_id": "test-task",
        "data": {"level": "passed", "review_issues": []},
        "summary": "Review passed",
        "warnings": [],
        "error": None,
        "duration_ms": 80,
        "attempt": 1,
    }


def word_export_envelope_ok(public_id: str, version_no: int) -> Dict[str, Any]:
    return {
        "success": True,
        "tool_name": "WordExportTool",
        "task_id": "test-task",
        "data": {
            "artifact_public_id": public_id,
            "version_no": version_no,
            "modified_section_ids": [],
        },
        "summary": f"Exported artifact v{version_no}",
        "warnings": [],
        "error": None,
        "duration_ms": 200,
        "attempt": 1,
    }


def format_check_envelope_ok() -> Dict[str, Any]:
    return {
        "success": True,
        "tool_name": "DocxFormatCheckTool",
        "task_id": "test-task",
        "data": {"level": "passed", "losses": []},
        "summary": "Format check passed",
        "warnings": [],
        "error": None,
        "duration_ms": 60,
        "attempt": 1,
    }


# ── Builders ─────────────────────────────────────────────────────


def build_existing_artifact_ref(
    *,
    artifact_public_id: str = "artifact-abc123",
    version_no: int = 1,
    section_package: Optional[Dict[str, Any]] = None,
    review_result: Optional[Dict[str, Any]] = None,
) -> ExistingArtifactRef:
    return ExistingArtifactRef(
        artifact_public_id=artifact_public_id,
        task_public_id="task-xyz789",
        version_no=version_no,
        source_artifact_id=None,
        superseded_artifact_ids=[],
        section_package=section_package or {
            "sections": [
                {"section_id": "s1", "title": "Section 1", "content": "abc"},
                {"section_id": "s2", "title": "Section 2", "content": "def"},
            ]
        },
        review_result=review_result or {"level": "passed", "rule_issues": []},
        last_format_check_result={"level": "passed", "losses": []},
    )


def build_modification_scope(
    *,
    kind: str = "modify_section",
    target_section_ids: Optional[List[str]] = None,
    request_text: str = "请修改 s1",
    locked_section_ids: Optional[List[str]] = None,
    allow_extra_sections: bool = False,
) -> ModificationScope:
    return ModificationScope(
        kind=kind,  # type: ignore[arg-type]
        target_section_ids=target_section_ids or ["s1"],
        request_text=request_text,
        locked_section_ids=locked_section_ids or [],
        allow_extra_sections=allow_extra_sections,
        new_artifact_idempotency_key=None,
    )


def build_intent(
    *,
    kind: str = "modify_section",
    target_section_ids: Optional[List[str]] = None,
    locked_section_ids: Optional[List[str]] = None,
    allow_extra_sections: bool = False,
    artifact_public_id: str = "artifact-abc123",
    version_no: int = 1,
    raw_user_message: str = "请修改 s1",
) -> IncrementalIntent:
    return IncrementalIntent(
        existing_artifact=build_existing_artifact_ref(
            artifact_public_id=artifact_public_id,
            version_no=version_no,
        ),
        scope=build_modification_scope(
            kind=kind,
            target_section_ids=target_section_ids,
            request_text=raw_user_message,
            locked_section_ids=locked_section_ids,
            allow_extra_sections=allow_extra_sections,
        ),
        confidence=0.9,
        raw_user_message=raw_user_message,
    )


def build_state(
    *,
    intent: Optional[IncrementalIntent] = None,
    incremental_agent_enabled: bool = True,
    locked_section_ids: Optional[List[str]] = None,
) -> TestPlanGraphState:
    state = make_empty_state(
        task_id="test-task",
        graph_run_id="run-001",
        incremental_agent_enabled=incremental_agent_enabled,
        locked_section_ids=locked_section_ids or [],
    )
    if intent is not None:
        # 保留 Pydantic 实例:agent_loop / event_emitter 都按 Pydantic 访问
        # (.scope / .existing_artifact / .confidence);LangGraph StateGraph
        # 自身负责 JSON dump/load。
        state["incremental_intent"] = intent
        state["source_artifact_public_id"] = intent.existing_artifact.artifact_public_id
        state["modification_idempotency_key"] = (
            f"test-idem-{intent.existing_artifact.artifact_public_id}"
        )
    return state


# ── Pytest fixtures ──────────────────────────────────────────────


@pytest.fixture
def make_fake_llm():
    def _factory(responses: List[Any]) -> FakeLLMClient:
        return FakeLLMClient(responses)
    return _factory


@pytest.fixture
def make_stub_adapter():
    def _factory(envelopes: Dict[str, List[Dict[str, Any]]] | None = None) -> StubToolAdapter:
        return StubToolAdapter(envelopes)
    return _factory
