"""Retirement contracts for the production Legacy Agent Orchestrator.

These tests intentionally distinguish orchestration-engine retirement from
legacy Context/LLM invocation. Shared tools and MIG_* compatibility paths are
allowed; production construction or dispatch of AgentOrchestrator is not.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest


APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def _source(relative_path: str) -> str:
    return (APP_ROOT / relative_path).read_text(encoding="utf-8")


def test_production_startup_does_not_mount_legacy_orchestrator() -> None:
    source = _source("main.py")
    assert "app.agent.orchestrator" not in source
    assert "agent_orchestrator" not in source
    assert "_StubCoordinator" not in source
    assert "legacy_orchestrator_mounted=false" in source
    assert "production_agent_engine=langgraph" in source


def test_api_dispatcher_is_langgraph_only() -> None:
    source = _source("agent_runtime/api_dispatcher.py")
    assert "LegacyOrchestratorProtocol" not in source
    assert "self._orchestrator" not in source
    assert "_legacy_orchestrator_enabled" not in source
    assert "UNSUPPORTED_LEGACY_TASK" in source


def test_worker_rejects_legacy_outbox_rows_without_dispatching() -> None:
    source = _source("services/agent_execution_worker.py")
    assert 'row_engine_type: str = str(row.engine_type or "legacy")' not in source
    assert "UNSUPPORTED_LEGACY_TASK" in source
    assert "migration-required" in source


def test_sse_is_observation_only_and_historical_actions_are_safe() -> None:
    from app.api.v1 import agent_tasks

    sse_source = inspect.getsource(agent_tasks.task_events_sse)
    module_source = inspect.getsource(agent_tasks)
    assert "dispatch_new_task" not in sse_source
    assert "app.agent.orchestrator" not in module_source
    assert "agent_orchestrator" not in module_source
    assert "MIGRATION_REQUIRED" in module_source


def test_new_task_persistence_never_defaults_to_legacy() -> None:
    message_source = _source("services/message_service.py")
    repository_source = _source("repositories/agent_task_repository.py")
    assert "EngineRouter" not in inspect.getsource(
        __import__(
            "app.services.message_service", fromlist=["MessageService"]
        ).MessageService._create_agent_task
    )
    assert 'engine_type = "langgraph"' in message_source
    assert 'task.engine_type or "legacy"' not in repository_source
    assert 'engine_type or "legacy"' not in message_source


def test_legacy_orchestrator_feature_flag_is_removed() -> None:
    source = _source("context_engine/feature_flags.py")
    resolver_source = _source("context_engine/freeze/resolver.py")
    assert "LEGACY_ORCHESTRATOR_ENABLED" not in source
    assert "legacy_orchestrator_enabled" not in source
    assert "LEGACY_ORCHESTRATOR_ENABLED" not in resolver_source


def test_legacy_orchestrator_implementation_is_deleted() -> None:
    assert not (APP_ROOT / "agent" / "orchestrator.py").exists()


def test_tool_proxy_preserves_task_flag_resolver() -> None:
    from app.agent_runtime.adapters.test_agent_tool_adapter import TestAgentToolAdapter

    adapter = object.__new__(TestAgentToolAdapter)
    adapter._session_factory = object()
    resolver = object()
    runtime = SimpleNamespace(
        task_id="task_public",
        task_internal_id=7,
        conversation_internal_id=8,
        user_internal_id=9,
        settings_service=object(),
        context_llm_invoker=None,
        llm_client=None,
        task_flag_resolver=resolver,
    )

    proxy = adapter._build_proxy(
        tool_name="RequirementParserTool",
        attempt=1,
        tool_call_id="tc_1",
        ctx_runtime=runtime,
        inputs={},
        graph_state={"task_id": "task_public"},
    )

    assert proxy.task_flag_resolver is resolver


@pytest.mark.asyncio
async def test_context_bridge_preserves_task_prompt_contract_and_public_task_id() -> None:
    from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge

    class RecordingInvoker:
        def __init__(self) -> None:
            self.request = None

        async def invoke(self, *, request, llm_task_profile, runtime_context):
            self.request = request
            return SimpleNamespace(
                value="ok", snapshot_public_id="snap_1", attempts=(), token_usage=None
            )

    invoker = RecordingInvoker()
    bridge = ContextInvokerBridge(invoker=invoker, enabled=True)
    runtime = SimpleNamespace(
        conversation_internal_id=8,
        conversation_public_id="conv_public",
        task_internal_id=7,
    )
    profile = SimpleNamespace(system_prompt="profile-specific-system")

    bound = bridge.bind(
        user_id=9,
        call_site="test.generate",
        task_id="task_public",
        runtime_context=runtime,
    )
    result = await bound.generate_with_profile(
        profile,
        "user content",
        system_prompt_override="task-specific-system",
        output_contract="strict-json-contract",
    )

    assert result.success is True
    assert invoker.request.task_id == "task_public"
    assert invoker.request.system_prompt == "task-specific-system"
    assert invoker.request.output_contract == "strict-json-contract"


def test_dynamic_agent_context_bridge_obeys_frozen_task_flag() -> None:
    source = _source("agent_runtime/dynamic_agent/executor.py")
    assert 'is_agent_context_migration_enabled(' in source
    assert 'resolver, "MIG_NARRATIVE"' in source


@pytest.mark.asyncio
async def test_failed_runtime_readiness_creates_no_task_or_outbox() -> None:
    from app.core.exceptions import LangGraphNotReadyError
    from app.services.message_service import MessageService

    service = object.__new__(MessageService)
    service._agent_runtime_ready = False
    service._agent_runtime_readiness_reason = "checkpointer_unavailable"

    with pytest.raises(LangGraphNotReadyError):
        await service._create_agent_task(
            conv=None,
            user_internal_id=1,
            content="生成测试方案",
            now=None,
            user_msg_dict={},
            intent_result=None,
        )


@pytest.mark.asyncio
async def test_health_exposes_retirement_and_readiness_state() -> None:
    from app.api.v1.health import health

    state = SimpleNamespace(
        probe_report=SimpleNamespace(
            langgraph_readiness=False,
            checkpointer_type="none",
        ),
        worker_started=False,
        langgraph_coordinator=None,
        api_dispatcher=object(),
        pending_legacy_tasks=3,
    )
    payload = await health(SimpleNamespace(app=SimpleNamespace(state=state)))
    runtime = payload["data"]["agent_runtime"]
    assert runtime == {
        "production_engine": "langgraph",
        "legacy_orchestrator_mounted": False,
        "langgraph_readiness": False,
        "worker_started": False,
        "coordinator_mounted": False,
        "dispatcher_mounted": True,
        "checkpointer_type": "none",
        "pending_legacy_tasks": 3,
    }


def test_completed_historical_task_detail_remains_readable() -> None:
    from app.services.agent_task_service import AgentTaskService

    task = SimpleNamespace(
        public_id="task-historical",
        task_type="test_plan_generation",
        status="completed",
        plan_json=[{"step_id": "done"}],
        task_context_json={"historical": True},
        review_result_json={"passed": True},
        active_run_id=None,
        runtime_status="completed",
        engine_type="legacy",
        graph_name=None,
        graph_version=None,
        started_at=None,
        completed_at=None,
    )
    detail = AgentTaskService._to_detail(task)
    assert detail["status"] == "completed"
    assert detail["plan"] == [{"step_id": "done"}]
    assert detail["execution_supported"] is False
    assert detail["migration_status"] == "historical-read-only"


def test_incomplete_historical_task_detail_requires_migration() -> None:
    from app.services.agent_task_service import AgentTaskService

    task = SimpleNamespace(
        public_id="task-historical-running",
        task_type="test_plan_generation",
        status="running",
        plan_json=[],
        task_context_json={},
        review_result_json=None,
        active_run_id=None,
        runtime_status="migration_required",
        engine_type="legacy",
        graph_name=None,
        graph_version=None,
        started_at=None,
        completed_at=None,
    )
    detail = AgentTaskService._to_detail(task)
    assert detail["execution_supported"] is False
    assert detail["migration_status"] == "migration-required"
