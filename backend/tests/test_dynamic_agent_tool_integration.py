from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.agent.atomic_capability_registry import AtomicCapabilityRegistry
from app.agent_runtime.dynamic_agent.executor import DynamicStepExecutor
from app.agent_runtime.dynamic_agent.planner import DynamicPlanner
from app.agent_runtime.dynamic_agent.plan_validator import DynamicPlanValidator
from app.agent_runtime.dynamic_agent.schemas import DynamicPlanStep
from app.agent_runtime.graph_runtime_service import GraphRuntimeService
from app.agent_runtime.graph_registry import GraphRegistry
from app.agent_runtime.graphs.dynamic_agent import (
    GRAPH_NAME_DYNAMIC_AGENT,
    GRAPH_VERSION_DYNAMIC_AGENT_V1,
)
from app.agent_runtime.runtime_context import RuntimeContext


@dataclass
class _FakeUploadedFile:
    public_id: str = "file_doc"
    user_id: int = 1
    conversation_id: int = 10
    file_ext: str = ".docx"
    deleted_at: Any = None


class _FakeFileResolver:
    def __init__(self, uploaded: _FakeUploadedFile | None) -> None:
        self.uploaded = uploaded
        self.calls: list[tuple[int, str]] = []

    async def __call__(self, *, user_id: int, file_public_id: str):
        self.calls.append((user_id, file_public_id))
        return self.uploaded


class _FakeToolAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, *, tool_name, inputs, ctx_runtime, graph_state=None, **_kw):
        self.calls.append(
            {
                "tool_name": tool_name,
                "inputs": dict(inputs),
                "ctx_runtime": ctx_runtime,
                "graph_state": dict(graph_state or {}),
            }
        )
        return {
            "success": True,
            "summary": "parsed document",
            "data": {
                "text_content": "Document body",
                "document_structure": [{"title": "Intro"}],
                "table_summaries": [{"summary": "table"}],
                "image_texts": [{"ocr_text": "image text"}],
            },
            "warnings": [],
            "error": None,
        }


class _NestedDocumentToolAdapter(_FakeToolAdapter):
    async def execute(self, *, tool_name, inputs, ctx_runtime, graph_state=None, **_kw):
        self.calls.append(
            {
                "tool_name": tool_name,
                "inputs": dict(inputs),
                "ctx_runtime": ctx_runtime,
                "graph_state": dict(graph_state or {}),
            }
        )
        return {
            "success": True,
            "summary": "parsed nested document",
            "data": {
                "requirement_analysis": {
                    "text_content": "Nested document body",
                    "document_structure": [{"title": "业务规则"}],
                    "tables": [{"summary": "reservation limits"}],
                    "images": [{"ocr_text": "canary"}],
                    "document_name": "NOVA_需求规格说明书.docx",
                }
            },
            "warnings": [],
            "error": None,
        }


class _NarrativeToolAdapter(_FakeToolAdapter):
    def last_terminal_event_id(self) -> str:
        return "evt_tool_terminal_1"

    async def execute(self, *, tool_name, inputs, ctx_runtime, graph_state=None, **_kw):
        result = await super().execute(
            tool_name=tool_name,
            inputs=inputs,
            ctx_runtime=ctx_runtime,
            graph_state=graph_state,
            **_kw,
        )
        result["tool_call_id"] = "RequirementParserTool-call-1"
        result["attempt"] = 1
        result["duration_ms"] = 120
        return result


class _FakeNarrativeLLM:
    is_context_engine_bridge = True

    async def generate_with_system(self, system_prompt, user_content, timeout_override=None, **_kwargs):
        return (
            "<NARRATIVE>"
            "Word 文档解析结果已经可用，接下来会把正文作为证据继续分析。"
            "</NARRATIVE>"
        )


class _FakeNarrativeBridge:
    available = True

    def __init__(self, llm: _FakeNarrativeLLM) -> None:
        self._llm = llm
        self.bind_calls: list[dict[str, Any]] = []

    def bind(self, **kwargs):
        self.bind_calls.append(dict(kwargs))
        return self._llm


class _NullSession:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return False


class _NullSink:
    async def emit(self, **_kw):
        return {"ok": True}


class _CapturingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, **kw):
        self.events.append(dict(kw))
        return {"event_id": f"evt_{len(self.events)}"}


class _NullCancel:
    def is_cancelled(self, _task_id):
        return False


def _ctx(
    tool_adapter: Any,
    *,
    context_llm_invoker: Any = None,
    event_sink: Any = None,
    llm_client: Any = None,
    settings_service: Any = None,
) -> RuntimeContext:
    return RuntimeContext(
        user_internal_id=1,
        task_internal_id=100,
        conversation_internal_id=10,
        session_factory=lambda: _NullSession(),
        settings_service=settings_service,
        event_sink=event_sink or _NullSink(),
        cancellation_service=_NullCancel(),
        clock=lambda: datetime.utcnow(),
        tool_adapter=tool_adapter,
        llm_client=llm_client,
        context_llm_invoker=context_llm_invoker,
        task_flag_resolver=SimpleNamespace(
            evaluate=lambda name: name
            in {"CONTEXT_ENGINE_AGENT_ENABLED", "MIG_NARRATIVE"}
        ),
    )


def _word_step() -> DynamicPlanStep:
    return DynamicPlanStep(
        step_id="step_1",
        title="Parse Word",
        action_type="tool",
        capability_key="word_document_parse",
        input_refs=["attachment:0"],
        success_criteria=["document_text_non_empty", "document_structure_non_empty"],
    )


def _evidence_step() -> DynamicPlanStep:
    return DynamicPlanStep(
        step_id="step_2",
        title="Analyze evidence",
        action_type="analysis",
        capability_key="evidence_analysis",
        input_refs=["step:step_1.output"],
        depends_on=["step_1"],
        success_criteria=["analysis_addresses_user_goal"],
    )


def _state() -> dict:
    return {
        "task_id": "task_dyn_tool",
        "goal": "Analyze this document",
        "attachment_refs": [
            {
                "ref": "attachment:0",
                "file_public_id": "file_doc",
                "file_ext": ".docx",
            }
        ],
    }


class _FakeContextBridge:
    available = True

    def __init__(self, value: Any = "Context Engine analysis") -> None:
        self.value = value
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            value=self.value,
            snapshot_public_id="ctx_snap_1",
            context_state_patch={"context_state": {"latest_snapshot_public_id": "ctx_snap_1"}},
            stats={"snapshot_public_id": "ctx_snap_1"},
        )


class _FakePlannerBridge(_FakeContextBridge):
    def __init__(self) -> None:
        super().__init__(
            {
                "goal": "Analyze this document",
                "revision": 1,
                "steps": [
                    {
                        "step_id": "step_1",
                        "title": "LLM selected parser",
                        "action_type": "tool",
                        "capability_key": "word_document_parse",
                        "input_refs": ["attachment:0"],
                        "depends_on": [],
                        "success_criteria": [
                            "document_text_non_empty",
                            "document_structure_non_empty",
                        ],
                    },
                    {
                        "step_id": "step_2",
                        "title": "LLM selected evidence analysis",
                        "action_type": "analysis",
                        "capability_key": "evidence_analysis",
                        "input_refs": ["step:step_1.output"],
                        "depends_on": ["step_1"],
                        "success_criteria": ["analysis_addresses_user_goal"],
                    },
                ],
            }
        )


class _RetryingPlannerBridge(_FakePlannerBridge):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            raise ValidationError.from_exception_data(
                "DynamicPlan",
                [
                    {
                        "type": "missing",
                        "loc": ("steps",),
                        "msg": "Field required",
                        "input": {},
                    }
                ],
            )
        return SimpleNamespace(
            value=self.value,
            snapshot_public_id="ctx_snap_1",
            context_state_patch={"context_state": {"latest_snapshot_public_id": "ctx_snap_1"}},
            stats={"snapshot_public_id": "ctx_snap_1"},
        )


class _FailingPlannerBridge:
    available = True

    async def generate(self, **_kwargs):
        raise RuntimeError("context.profile.no_call_site_mapping")


class _FailingContextBridge:
    available = True

    async def generate(self, **_kwargs):
        raise RuntimeError("context.source.task_not_found")


def test_planner_accepts_string_attachment_refs_for_docx_tasks() -> None:
    plan = DynamicPlanner(AtomicCapabilityRegistry.default()).plan(
        {
            "goal": "Analyze this document",
            "target_capability": "document_qa",
            "operation": "analyze",
            "attachment_refs": ["file_doc"],
        }
    )

    assert [step.capability_key for step in plan.steps] == [
        "word_document_parse",
        "evidence_analysis",
    ]
    assert plan.steps[0].input_refs == ["attachment:0"]


def test_planner_accepts_uploaded_docx_ext_without_dot() -> None:
    plan = DynamicPlanner(AtomicCapabilityRegistry.default()).plan(
        {
            "goal": "Analyze this document",
            "target_capability": "document_qa",
            "operation": "analyze",
            "attachment_refs": [
                {
                    "ref": "attachment:0",
                    "file_public_id": "file_doc",
                    "file_ext": "docx",
                    "original_name": "requirement.docx",
                }
            ],
        }
    )

    assert [step.capability_key for step in plan.steps] == [
        "word_document_parse",
        "evidence_analysis",
    ]
    assert plan.steps[0].input_refs == ["attachment:0"]


@pytest.mark.asyncio
async def test_planner_logs_deterministic_fallback_without_context_bridge(caplog) -> None:
    caplog.set_level(
        logging.WARNING,
        logger="app.agent_runtime.dynamic_agent.planner",
    )

    plan = await DynamicPlanner(AtomicCapabilityRegistry.default()).aplan(
        {
            "task_id": "task_dyn_plan_fallback",
            "goal": "Analyze this document",
            "target_capability": "document_qa",
            "operation": "analyze",
            "attachment_refs": [
                {
                    "ref": "attachment:0",
                    "file_public_id": "file_doc",
                    "file_ext": "docx",
                }
            ],
        }
    )

    assert [step.capability_key for step in plan.steps] == [
        "word_document_parse",
        "evidence_analysis",
    ]
    assert any(
        "FALLBACK_USED | component=dynamic_agent.planner" in record.message
        and "reason=context_bridge_missing" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_planner_falls_back_when_context_bridge_raises(caplog) -> None:
    caplog.set_level(
        logging.WARNING,
        logger="app.agent_runtime.dynamic_agent.planner",
    )

    plan = await DynamicPlanner(AtomicCapabilityRegistry.default()).aplan(
        {
            "task_id": "task_dyn_plan_bridge_error",
            "goal": "Analyze this document",
            "target_capability": "document_qa",
            "operation": "analyze",
            "attachment_refs": [
                {
                    "ref": "attachment:0",
                    "file_public_id": "file_doc",
                    "file_ext": ".docx",
                }
            ],
        },
        runtime_context=SimpleNamespace(
            context_llm_invoker=_FailingPlannerBridge(),
            user_internal_id=1,
            task_internal_id=100,
        ),
    )

    assert [step.capability_key for step in plan.steps] == [
        "word_document_parse",
        "evidence_analysis",
    ]
    assert any(
        "FALLBACK_USED | component=dynamic_agent.planner" in record.message
        and "reason=llm_planner_exception:RuntimeError" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_planner_retries_contract_parse_failures_before_fallback(caplog) -> None:
    caplog.set_level(
        logging.WARNING,
        logger="app.agent_runtime.dynamic_agent.planner",
    )
    bridge = _RetryingPlannerBridge(failures=2)

    plan = await DynamicPlanner(AtomicCapabilityRegistry.default()).aplan(
        {
            "task_id": "task_dyn_plan_retry",
            "goal": "Analyze this document",
            "target_capability": "document_qa",
            "operation": "analyze",
            "attachment_refs": [
                {
                    "ref": "attachment:0",
                    "file_public_id": "file_doc",
                    "file_ext": ".docx",
                }
            ],
        },
        runtime_context=SimpleNamespace(
            context_llm_invoker=bridge,
            user_internal_id=1,
            task_internal_id=100,
            conversation_internal_id=10,
        ),
    )

    assert plan.steps[0].title == "LLM selected parser"
    assert len(bridge.calls) == 3
    assert bridge.calls[1]["task_state_ref"]["planner_attempt"] == 2
    assert "Previous planner output could not be parsed" in bridge.calls[1]["user_content"]
    assert any("PLANNER_RETRY | component=dynamic_agent.planner" in record.message for record in caplog.records)
    assert not any("FALLBACK_USED | component=dynamic_agent.planner" in record.message for record in caplog.records)


def test_planner_adds_knowledge_search_for_document_standard_compare() -> None:
    plan = DynamicPlanner(AtomicCapabilityRegistry.default()).plan(
        {
            "goal": "Check whether this document follows company standards",
            "target_capability": "document_qa",
            "operation": "compare",
            "attachment_refs": [
                {
                    "ref": "attachment:0",
                    "file_public_id": "file_doc",
                    "file_ext": ".docx",
                }
            ],
            "retrieval_plan_snapshot": {"maas": "required"},
        }
    )

    assert [step.capability_key for step in plan.steps] == [
        "word_document_parse",
        "knowledge_search",
        "evidence_analysis",
    ]
    assert plan.steps[2].depends_on == ["step_1", "step_2"]


def test_plan_validator_rejects_knowledge_search_when_retrieval_disallows() -> None:
    registry = AtomicCapabilityRegistry.default()
    plan = DynamicPlanner(registry).plan(
        {
            "goal": "Check whether this document follows company standards",
            "target_capability": "document_qa",
            "operation": "compare",
            "attachment_refs": [
                {
                    "ref": "attachment:0",
                    "file_public_id": "file_doc",
                    "file_ext": ".docx",
                }
            ],
            "retrieval_plan_snapshot": {"maas": "required"},
        }
    )
    state = _state()
    state["retrieval_plan_snapshot"] = {"maas": "off"}

    result = DynamicPlanValidator(registry).validate(plan, state)

    assert result.valid is False
    assert "retrieval_policy_disallows:knowledge_search" in result.errors


@pytest.mark.asyncio
async def test_word_document_parse_calls_requirement_parser_tool() -> None:
    adapter = _FakeToolAdapter()
    resolver = _FakeFileResolver(_FakeUploadedFile())

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_ctx(adapter),
        file_resolver=resolver,
    ).execute(_word_step(), _state())

    assert observation.status == "success"
    assert observation.facts["text_length"] == len("Document body")
    assert observation.facts["has_structure"] is True
    assert adapter.calls[0]["tool_name"] == "RequirementParserTool"
    assert adapter.calls[0]["inputs"] == {"requirement_file_id": "file_doc"}
    assert resolver.calls == [(1, "file_doc")]


@pytest.mark.asyncio
async def test_word_document_parse_accepts_uploaded_docx_ext_without_dot() -> None:
    adapter = _FakeToolAdapter()
    resolver = _FakeFileResolver(_FakeUploadedFile(file_ext="docx"))

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_ctx(adapter),
        file_resolver=resolver,
    ).execute(_word_step(), _state())

    assert observation.status == "success"
    assert adapter.calls[0]["tool_name"] == "RequirementParserTool"


@pytest.mark.asyncio
async def test_word_document_parse_unwraps_nested_requirement_analysis_data() -> None:
    adapter = _NestedDocumentToolAdapter()
    resolver = _FakeFileResolver(_FakeUploadedFile())

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_ctx(adapter),
        file_resolver=resolver,
    ).execute(_word_step(), _state())

    assert observation.status == "success"
    assert observation.facts["text_length"] == len("Nested document body")
    assert observation.facts["has_structure"] is True
    assert observation.data["text_content"] == "Nested document body"
    assert observation.data["document_structure"] == [{"title": "业务规则"}]
    assert observation.data["table_summaries"] == [{"summary": "reservation limits"}]
    assert observation.data["image_texts"] == [{"ocr_text": "canary"}]


@pytest.mark.asyncio
async def test_word_document_parse_emits_tool_narrative_when_llm_available(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.agent_runtime.dynamic_agent.tool_handlers.get_feature_flags",
        lambda: SimpleNamespace(
            phase29b_tool_narrative_enabled=True,
            phase29b_narrative_timeout_seconds=5,
            phase29b_narrative_repair_attempts=0,
        ),
    )
    adapter = _NarrativeToolAdapter()
    resolver = _FakeFileResolver(_FakeUploadedFile())
    sink = _CapturingSink()
    bridge = _FakeNarrativeBridge(_FakeNarrativeLLM())

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_ctx(
                adapter,
                event_sink=sink,
                context_llm_invoker=bridge,
        ),
        file_resolver=resolver,
    ).execute(_word_step(), _state())

    assert observation.status == "success"
    event_types = [event["event_type"] for event in sink.events]
    assert "tool_narrative_started" in event_types
    assert "tool_narrative_update" in event_types
    update = next(
        event for event in sink.events if event["event_type"] == "tool_narrative_update"
    )
    assert update["payload"]["source_tool_call_id"] == "RequirementParserTool-call-1"
    assert update["payload"]["source_event_id"] == "evt_tool_terminal_1"
    assert update["payload"]["narrative_source"] == "llm"


@pytest.mark.asyncio
async def test_word_document_parse_skips_tool_narrative_llm_when_user_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.agent_runtime.dynamic_agent.tool_handlers.get_feature_flags",
        lambda: SimpleNamespace(
            phase29b_tool_narrative_enabled=True,
            phase29b_narrative_timeout_seconds=5,
            phase29b_narrative_repair_attempts=0,
        ),
    )

    class _NarrativeDisabled:
        async def tool_card_narrative_enabled(self) -> bool:
            return False

    adapter = _NarrativeToolAdapter()
    bridge = _FakeNarrativeBridge(_FakeNarrativeLLM())
    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_ctx(
            adapter,
            event_sink=_CapturingSink(),
            context_llm_invoker=bridge,
            settings_service=_NarrativeDisabled(),
        ),
        file_resolver=_FakeFileResolver(_FakeUploadedFile()),
    ).execute(_word_step(), _state())

    assert observation.status == "success"
    assert bridge.bind_calls == []


@pytest.mark.asyncio
async def test_evidence_analysis_uses_context_engine_bridge_when_available() -> None:
    bridge = _FakeContextBridge({"analysis": "The document describes onboarding."})
    state = _state()
    state["observations"] = [
        {
            "step_id": "step_1",
            "capability_key": "word_document_parse",
            "status": "success",
            "summary": "parsed document",
            "facts": {"text_length": 123, "has_structure": True},
        }
    ]
    state["tool_results"] = {
        "step_1": {
            "status": "success",
            "summary": "parsed document",
            "facts": {"text_length": 123, "has_structure": True},
            "data": {"text_content": "Document body"},
        }
    }

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_ctx(_FakeToolAdapter(), context_llm_invoker=bridge),
    ).execute(_evidence_step(), state)

    assert observation.status == "success"
    assert observation.summary == "The document describes onboarding."
    assert observation.data["context_state"]["latest_snapshot_public_id"] == "ctx_snap_1"
    assert bridge.calls[0]["call_site"] == "ce.pilot.summarize"
    assert bridge.calls[0]["current_node"] == "evidence_analysis"
    assert bridge.calls[0]["runtime_context"].context_llm_invoker is bridge
    evidence = bridge.calls[0]["task_state_ref"]["tool_result_evidence"]
    assert evidence["step_1"]["data"]["text_content"] == "Document body"


@pytest.mark.asyncio
async def test_dynamic_graph_uses_context_engine_bridge_for_planning() -> None:
    adapter = _FakeToolAdapter()
    resolver = _FakeFileResolver(_FakeUploadedFile())
    planner_bridge = _FakePlannerBridge()
    registry = GraphRegistry.build_default_v2_v3()
    runtime = GraphRuntimeService(registry)

    result = await runtime.ainvoke(
        {
            "task_id": "task_dyn_graph_llm_plan",
            "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
            "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
            "goal": "Analyze this document",
            "target_capability": "document_qa",
            "operation": "analyze",
            "attachment_refs": [
                {
                    "ref": "attachment:0",
                    "file_public_id": "file_doc",
                    "file_ext": ".docx",
                }
            ],
        },
        config={
            "configurable": {
                "thread_id": "task_dyn_graph_llm_plan",
                "runtime_context": _ctx(
                    adapter,
                    context_llm_invoker=planner_bridge,
                ),
                "dynamic_file_resolver": resolver,
            }
        },
    )

    assert result["task_status"] == "completed"
    assert result["plan"]["steps"][0]["title"] == "LLM selected parser"
    assert planner_bridge.calls[0]["call_site"] == "dynamic_agent.planner"
    assert planner_bridge.calls[0]["current_node"] == "create_plan"
    assert planner_bridge.calls[0]["task_id"] == "task_dyn_graph_llm_plan"
    assert planner_bridge.calls[0]["runtime_context"].context_llm_invoker is planner_bridge


@pytest.mark.asyncio
async def test_dynamic_graph_emits_understanding_summary_in_plan_event() -> None:
    adapter = _FakeToolAdapter()
    resolver = _FakeFileResolver(_FakeUploadedFile())
    sink = _CapturingSink()
    registry = GraphRegistry.build_default_v2_v3()
    runtime = GraphRuntimeService(registry)

    await runtime.ainvoke(
        {
            "task_id": "task_dyn_graph_summary",
            "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
            "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
            "goal": "总结这个文档的内容",
            "target_capability": "document_qa",
            "operation": "summarize",
            "attachment_refs": [
                {
                    "ref": "attachment:0",
                    "file_public_id": "file_doc",
                    "file_ext": ".docx",
                }
            ],
        },
        config={
            "configurable": {
                "thread_id": "task_dyn_graph_summary",
                "runtime_context": _ctx(adapter, event_sink=sink),
                "dynamic_file_resolver": resolver,
            }
        },
    )

    plan_event = next(event for event in sink.events if event["event_type"] == "plan_created")
    assert plan_event["payload"]["goal"] == "总结这个文档的内容"
    assert plan_event["payload"]["operation"] == "summarize"
    assert (
        plan_event["payload"]["understanding_summary"]
        == "根据上传文档总结文档内容。"
    )
    assert [step["step_id"] for step in plan_event["payload"]["steps"]] == [
        "step_1",
        "step_2",
        "verify_goal",
        "synthesize_answer",
    ]


@pytest.mark.asyncio
async def test_evidence_analysis_keeps_deterministic_fallback_without_context_engine(
    caplog,
) -> None:
    caplog.set_level(
        logging.WARNING,
        logger="app.agent_runtime.dynamic_agent.executor",
    )
    state = _state()
    state["observations"] = [
        {
            "step_id": "step_1",
            "capability_key": "word_document_parse",
            "status": "success",
            "summary": "parsed document",
        }
    ]

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
    ).execute(_evidence_step(), state)

    assert observation.status == "success"
    assert observation.summary == "parsed document"
    assert any(
        "FALLBACK_USED | component=dynamic_agent.executor" in record.message
        and "to=deterministic_summary" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_evidence_analysis_fallback_uses_parsed_document_text() -> None:
    state = _state()
    state["observations"] = [
        {
            "step_id": "step_1",
            "capability_key": "word_document_parse",
            "status": "success",
            "summary": "parsed document",
        }
    ]
    state["tool_results"] = {
        "step_1": {
            "status": "success",
            "summary": "parsed document",
            "data": {
                "text_content": "Real requirement body about refunds and order cancellation.",
                "document_structure": [{"title": "Refund flow"}],
            },
        }
    }

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
    ).execute(_evidence_step(), state)

    assert observation.status == "success"
    assert "Real requirement body" in observation.summary
    assert "parsed document" not in observation.summary


@pytest.mark.asyncio
async def test_evidence_analysis_falls_back_when_context_bridge_raises(caplog) -> None:
    caplog.set_level(
        logging.WARNING,
        logger="app.agent_runtime.dynamic_agent.executor",
    )
    state = _state()
    state["observations"] = [
        {
            "step_id": "step_1",
            "capability_key": "word_document_parse",
            "status": "success",
            "summary": "文档介绍在线学习与考试测评平台。",
        }
    ]

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_ctx(
            _FakeToolAdapter(),
            context_llm_invoker=_FailingContextBridge(),
        ),
    ).execute(_evidence_step(), state)

    assert observation.status == "success"
    assert observation.summary == "文档介绍在线学习与考试测评平台。"
    assert any(
        "FALLBACK_USED | component=dynamic_agent.executor" in record.message
        and "reason=context_bridge_exception:RuntimeError" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_word_document_parse_denies_cross_conversation_file() -> None:
    adapter = _FakeToolAdapter()
    resolver = _FakeFileResolver(_FakeUploadedFile(conversation_id=99))

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_ctx(adapter),
        file_resolver=resolver,
    ).execute(_word_step(), _state())

    assert observation.status == "failed"
    assert observation.facts["error_code"] == "FILE_CONVERSATION_MISMATCH"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_word_document_parse_denies_deleted_file() -> None:
    adapter = _FakeToolAdapter()
    resolver = _FakeFileResolver(_FakeUploadedFile(deleted_at=object()))

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_ctx(adapter),
        file_resolver=resolver,
    ).execute(_word_step(), _state())

    assert observation.status == "failed"
    assert observation.facts["error_code"] == "FILE_DELETED"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_word_document_parse_denies_unsupported_media() -> None:
    adapter = _FakeToolAdapter()
    resolver = _FakeFileResolver(_FakeUploadedFile(file_ext=".pdf"))

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_ctx(adapter),
        file_resolver=resolver,
    ).execute(_word_step(), _state())

    assert observation.status == "failed"
    assert observation.facts["error_code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_word_document_parse_accepts_string_attachment_refs() -> None:
    adapter = _FakeToolAdapter()
    resolver = _FakeFileResolver(_FakeUploadedFile())
    state = _state()
    state["attachment_refs"] = ["file_doc"]

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_ctx(adapter),
        file_resolver=resolver,
    ).execute(_word_step(), state)

    assert observation.status == "success"
    assert adapter.calls[0]["inputs"] == {"requirement_file_id": "file_doc"}


@pytest.mark.asyncio
async def test_default_file_resolver_queries_current_user_and_excludes_deleted(monkeypatch) -> None:
    from app.agent_runtime.dynamic_agent.tool_handlers import resolve_uploaded_file

    captured = {}

    class _Repo:
        def __init__(self, session):
            captured["session"] = session

        async def get_by_public_id(self, user_id, file_public_id):
            captured["user_id"] = user_id
            captured["file_public_id"] = file_public_id
            return _FakeUploadedFile()

    monkeypatch.setattr(
        "app.repositories.file_repository.FileRepository",
        _Repo,
    )

    uploaded = await resolve_uploaded_file(
        user_id=7,
        file_public_id="file_public",
        ctx_runtime=SimpleNamespace(session_factory=lambda: _NullSession()),
    )

    assert uploaded is not None
    assert captured["user_id"] == 7
    assert captured["file_public_id"] == "file_public"


@pytest.mark.asyncio
async def test_dynamic_graph_uses_runtime_tool_adapter_for_word_parse() -> None:
    adapter = _FakeToolAdapter()
    resolver = _FakeFileResolver(_FakeUploadedFile())
    registry = GraphRegistry.build_default_v2_v3()
    runtime = GraphRuntimeService(registry)

    result = await runtime.ainvoke(
        {
            "task_id": "task_dyn_graph_tool",
            "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
            "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
            "goal": "Analyze this document",
            "target_capability": "document_qa",
            "operation": "analyze",
            "attachment_refs": [
                {
                    "ref": "attachment:0",
                    "file_public_id": "file_doc",
                    "file_ext": ".docx",
                }
            ],
        },
        config={
            "configurable": {
                "thread_id": "task_dyn_graph_tool",
                "runtime_context": _ctx(adapter),
                "dynamic_file_resolver": resolver,
            }
        },
    )

    assert result["task_status"] == "completed"
    assert result["verification_status"] == "COMPLETE"
    assert adapter.calls[0]["tool_name"] == "RequirementParserTool"
    assert result["tool_results"]["step_1"]["data"]["text_content"] == "Document body"


@pytest.mark.asyncio
async def test_dynamic_graph_emits_dynamic_plan_and_step_events() -> None:
    adapter = _FakeToolAdapter()
    resolver = _FakeFileResolver(_FakeUploadedFile())
    sink = _CapturingSink()
    registry = GraphRegistry.build_default_v2_v3()
    runtime = GraphRuntimeService(registry)

    result = await runtime.ainvoke(
        {
            "task_id": "task_dyn_graph_events",
            "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
            "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
            "goal": "Analyze this document",
            "target_capability": "document_qa",
            "operation": "analyze",
            "attachment_refs": [
                {
                    "ref": "attachment:0",
                    "file_public_id": "file_doc",
                    "file_ext": ".docx",
                }
            ],
        },
        config={
            "configurable": {
                "thread_id": "task_dyn_graph_events",
                "runtime_context": _ctx(adapter, event_sink=sink),
                "dynamic_file_resolver": resolver,
            }
        },
    )

    event_types = [event["event_type"] for event in sink.events]
    assert result["task_status"] == "completed"
    assert "plan_created" in event_types
    assert event_types.count("plan_step_started") == 4
    assert event_types.count("plan_step_completed") == 4
    assert event_types.count("agent_observation_update") == 2
    assert "task_completed" in event_types
    plan_event = next(event for event in sink.events if event["event_type"] == "plan_created")
    assert plan_event["payload"]["plan"]["revision"] == 1
    assert plan_event["payload"]["plan"]["steps"][0]["step_id"] == "step_1"
    assert [step["step_id"] for step in plan_event["payload"]["steps"]] == [
        "step_1",
        "step_2",
        "verify_goal",
        "synthesize_answer",
    ]
    observation_titles = [
        event["title"]
        for event in sink.events
        if event["event_type"] == "agent_observation_update"
    ]
    assert observation_titles == ["检查分析结果", "生成最终答复"]
    completed_event = next(event for event in sink.events if event["event_type"] == "task_completed")
    assert completed_event["payload"]["summary"]


@pytest.mark.asyncio
async def test_knowledge_search_uses_tool_adapter_when_retrieval_plan_allows() -> None:
    class _KnowledgeAdapter(_FakeToolAdapter):
        async def execute(self, *, tool_name, inputs, ctx_runtime, graph_state=None, **_kw):
            self.calls.append({"tool_name": tool_name, "inputs": dict(inputs)})
            return {
                "success": True,
                "summary": "knowledge found",
                "data": {"hits": [{"title": "Standard"}], "hit_count": 1},
                "warnings": [],
                "error": None,
            }

    adapter = _KnowledgeAdapter()
    step = DynamicPlanStep(
        step_id="step_2",
        title="Search knowledge",
        action_type="tool",
        capability_key="knowledge_search",
        input_refs=[],
        success_criteria=["knowledge_result_available"],
    )

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_ctx(adapter),
    ).execute(
        step,
        {
            "goal": "Check company standards",
            "retrieval_plan_snapshot": {"maas": "required", "top_k": 3},
        },
    )

    assert observation.status == "success"
    assert observation.data["hit_count"] == 1
    assert adapter.calls == [
        {
            "tool_name": "KnowledgeSearchTool",
            "inputs": {"query": "Check company standards", "top_k": 3},
        }
    ]


@pytest.mark.asyncio
async def test_knowledge_search_fails_closed_when_retrieval_plan_disallows() -> None:
    adapter = _FakeToolAdapter()
    step = DynamicPlanStep(
        step_id="step_2",
        title="Search knowledge",
        action_type="tool",
        capability_key="knowledge_search",
        input_refs=[],
        success_criteria=["knowledge_result_available"],
    )

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_ctx(adapter),
    ).execute(
        step,
        {
            "goal": "Check standards",
            "retrieval_plan_snapshot": {"maas": "off"},
        },
    )

    assert observation.status == "failed"
    assert observation.facts["error_code"] == "KNOWLEDGE_SEARCH_NOT_ALLOWED"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_knowledge_search_reports_maas_strict_no_hit_explicitly() -> None:
    class _NoHitAdapter(_FakeToolAdapter):
        async def execute(self, *, tool_name, inputs, ctx_runtime, graph_state=None, **_kw):
            self.calls.append({"tool_name": tool_name, "inputs": dict(inputs)})
            return {
                "success": True,
                "summary": "knowledge search completed",
                "data": {"hits": [], "hit_count": 0},
                "warnings": [],
                "error": None,
            }

    adapter = _NoHitAdapter()
    step = DynamicPlanStep(
        step_id="step_2",
        title="Search knowledge",
        action_type="tool",
        capability_key="knowledge_search",
        input_refs=[],
        success_criteria=["knowledge_result_available"],
    )

    observation = await DynamicStepExecutor(
        AtomicCapabilityRegistry.default(),
        runtime_context=_ctx(adapter),
    ).execute(
        step,
        {
            "goal": "Check company standards",
            "knowledge_mode_snapshot": "MAAS_STRICT",
        },
    )

    assert observation.status == "success"
    assert observation.facts["hit_count"] == 0
    assert "No knowledge hits" in observation.summary
