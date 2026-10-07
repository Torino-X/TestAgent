"""Stage 1 contracts for post-retirement Context Engine isolation.

These tests cover only the defects that remain after the Legacy orchestrator
retirement.  Prompt/output-contract preservation already has a retirement
contract and is intentionally not duplicated here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_new_task_semantic_snapshot_uses_effective_runtime_flags() -> None:
    from app.context_engine.feature_flags import ContextEngineFeatureFlags
    from app.context_engine.freeze.profiles import snapshot_task_semantic_flags

    flags = ContextEngineFeatureFlags(
        context_engine_agent_enabled=True,
        context_memory_read_enabled=True,
        context_memory_write_enabled=True,
        context_retrieval_enabled=True,
        context_dense_retrieval_enabled=True,
        context_hybrid_fusion_enabled=True,
        context_rerank_enabled=True,
        mig_review=True,
        mig_repair=True,
        mig_generate=True,
        mig_preparation=True,
        mig_incremental=True,
        mig_summary=True,
        mig_narrative=True,
    )

    snapshot = snapshot_task_semantic_flags(flags)

    assert len(snapshot) == 23
    assert snapshot["CONTEXT_ENGINE_AGENT_ENABLED"] is True
    assert snapshot["CONTEXT_MEMORY_READ_ENABLED"] is True
    assert snapshot["CONTEXT_MEMORY_WRITE_ENABLED"] is True
    assert snapshot["MIG_GENERATE"] is True
    assert snapshot["MIG_NARRATIVE"] is True
    assert snapshot["MIG_CHAT"] is False


def test_feature_flags_read_dotenv_when_process_environment_is_not_exported(monkeypatch, tmp_path) -> None:
    """`.env` flags must feed the freeze snapshot even without shell exports."""
    import app.context_engine.feature_flags as feature_flags

    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "CONTEXT_ENGINE_ENABLED=1\n"
        "CONTEXT_ENGINE_AGENT_ENABLED=1\n"
        "MIG_GENERATE=1\n",
        encoding="utf-8",
    )
    for name in (
        "CONTEXT_ENGINE_ENABLED",
        "CONTEXT_ENGINE_AGENT_ENABLED",
        "MIG_GENERATE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(feature_flags, "_DOTENV_PATH", dotenv_path)
    feature_flags._read_dotenv_flags.cache_clear()

    flags = feature_flags.get_context_engine_flags()

    assert flags.context_engine_enabled is True
    assert flags.context_engine_agent_enabled is True
    assert flags.mig_generate is True


def test_full_chain_mode_rejects_an_incomplete_migration_configuration(monkeypatch, tmp_path) -> None:
    """Release mode must not silently freeze a partly-legacy new task."""
    import app.context_engine.feature_flags as feature_flags

    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "CONTEXT_ENGINE_ENABLED=1\n"
        "CONTEXT_ENGINE_FULL_CHAIN_REQUIRED=1\n"
        "CONTEXT_ENGINE_AGENT_ENABLED=1\n"
        "MIG_REVIEW=1\n"
        "MIG_REPAIR=1\n"
        # MIG_GENERATE is intentionally absent.
        "MIG_PREPARATION=1\n"
        "MIG_INCREMENTAL=1\n"
        "MIG_CHAT=1\n"
        "MIG_SUMMARY=1\n"
        "MIG_NARRATIVE=1\n",
        encoding="utf-8",
    )
    from app.context_engine.freeze.profiles import FROZEN_FLAG_KEYS

    for name in set(FROZEN_FLAG_KEYS) | {
        "CONTEXT_ENGINE_ENABLED",
        "CONTEXT_ENGINE_FULL_CHAIN_REQUIRED",
    }:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(feature_flags, "_DOTENV_PATH", dotenv_path)
    feature_flags._read_dotenv_flags.cache_clear()

    with pytest.raises(
        feature_flags.ContextEngineReleaseConfigurationError,
        match="MIG_GENERATE",
    ):
        feature_flags.validate_full_chain_configuration()


def test_full_chain_mode_accepts_an_explicit_complete_configuration(monkeypatch, tmp_path) -> None:
    import app.context_engine.feature_flags as feature_flags
    from app.context_engine.freeze.profiles import FROZEN_FLAG_KEYS

    dotenv_path = tmp_path / ".env"
    values = {
        "CONTEXT_ENGINE_ENABLED": "1",
        "CONTEXT_ENGINE_FULL_CHAIN_REQUIRED": "1",
        **{name: "1" for name in FROZEN_FLAG_KEYS},
    }
    dotenv_path.write_text(
        "".join(f"{name}={value}\n" for name, value in sorted(values.items())),
        encoding="utf-8",
    )
    for name in set(FROZEN_FLAG_KEYS) | set(values):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(feature_flags, "_DOTENV_PATH", dotenv_path)
    feature_flags._read_dotenv_flags.cache_clear()

    feature_flags.validate_full_chain_configuration()


def test_agent_master_flag_gates_every_task_migration_flag() -> None:
    from app.context_engine.feature_flags import is_agent_context_migration_enabled

    class Resolver:
        def __init__(self, *, agent_enabled: bool, migration_enabled: bool) -> None:
            self.agent_enabled = agent_enabled
            self.migration_enabled = migration_enabled

        def evaluate(self, name: str) -> bool:
            if name == "CONTEXT_ENGINE_AGENT_ENABLED":
                return self.agent_enabled
            if name == "MIG_GENERATE":
                return self.migration_enabled
            raise AssertionError(name)

    assert not is_agent_context_migration_enabled(
        Resolver(agent_enabled=False, migration_enabled=True), "MIG_GENERATE"
    )
    assert is_agent_context_migration_enabled(
        Resolver(agent_enabled=True, migration_enabled=True), "MIG_GENERATE"
    )


def test_agent_migration_without_frozen_resolver_fails_closed(monkeypatch) -> None:
    from app.context_engine.feature_flags import is_agent_context_migration_enabled

    monkeypatch.setenv("CONTEXT_ENGINE_AGENT_ENABLED", "1")
    monkeypatch.setenv("MIG_GENERATE", "1")

    assert not is_agent_context_migration_enabled(None, "MIG_GENERATE")


@pytest.mark.asyncio
async def test_production_runtime_resolver_load_failure_is_fail_closed() -> None:
    from app.agent_runtime.production_runtime_context_factory import (
        ProductionRuntimeContextFactory,
    )

    @asynccontextmanager
    async def broken_session_factory():
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover

    factory = object.__new__(ProductionRuntimeContextFactory)
    factory._session_factory = broken_session_factory

    with pytest.raises(RuntimeError, match="task flag resolver"):
        await factory._load_task_flag_resolver(
            task_internal_id=7,
            state={"engine_type": "langgraph"},
        )


@pytest.mark.asyncio
async def test_bridge_normalizes_internal_task_id_to_runtime_public_id() -> None:
    from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge

    class RecordingInvoker:
        request = None

        async def invoke(self, *, request, llm_task_profile, runtime_context):
            self.request = request
            return SimpleNamespace(
                value="ok",
                snapshot_public_id="snap_1",
                attempts=(),
                token_usage=None,
            )

    invoker = RecordingInvoker()
    bridge = ContextInvokerBridge(invoker=invoker, enabled=True)
    runtime = SimpleNamespace(
        task_internal_id=7,
        task_public_id="task_public_7",
        conversation_internal_id=8,
        conversation_public_id="conv_public_8",
        project_context={"workspace_key": "project:proj_public_8"},
    )

    result = await bridge.generate(
        user_id=9,
        call_site="test.generate",
        llm_task_profile=SimpleNamespace(system_prompt="system"),
        task_id=7,
        conversation_id=8,
        runtime_context=runtime,
    )

    assert result is not None
    assert invoker.request.task_id == "task_public_7"
    assert invoker.request.conversation_id == "8"
    assert invoker.request.conversation_public_id == "conv_public_8"
    assert invoker.request.workspace_key == "project:proj_public_8"


@pytest.mark.asyncio
async def test_bound_bridge_supports_narrative_generate_with_system() -> None:
    """MIG_NARRATIVE must not fail because the bound bridge lacks this API."""
    from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge

    class RecordingInvoker:
        request = None

        async def invoke(self, *, request, llm_task_profile, runtime_context):
            self.request = request
            assert llm_task_profile.system_prompt == "Narrative system rule"
            return SimpleNamespace(value="Narrative response", snapshot_public_id="snap_2", attempts=())

    invoker = RecordingInvoker()
    bridge = ContextInvokerBridge(invoker=invoker, enabled=True)
    runtime = SimpleNamespace(
        task_internal_id=7,
        task_public_id="task_public_7",
        conversation_internal_id=8,
        conversation_public_id="conv_public_8",
    )

    bound = bridge.bind(
        user_id=9,
        call_site="test_plan.tool_narrative",
        task_id=7,
        runtime_context=runtime,
    )
    result = await bound.generate_with_system(
        "Narrative system rule",
        "Only describe completed facts.",
        timeout_override=12,
        output_contract="Return exactly one <NARRATIVE>...</NARRATIVE> block.",
    )

    assert result == "Narrative response"
    assert invoker.request.call_site == "test_plan.tool_narrative"
    assert invoker.request.system_prompt == "Narrative system rule"
    assert invoker.request.output_contract == "Return exactly one <NARRATIVE>...</NARRATIVE> block."
    assert invoker.request.task_id == "task_public_7"


def test_compaction_persists_runtime_internal_ids_for_public_request_ids() -> None:
    from app.context_engine.compression.preflight_service import (
        _optional_internal_identity,
        _required_internal_identity,
    )

    runtime = SimpleNamespace(
        user_internal_id=3,
        conversation_internal_id=8,
        task_internal_id=7,
    )

    assert _required_internal_identity(runtime, "user_internal_id", "user_public") == 3
    assert (
        _optional_internal_identity(runtime, "conversation_internal_id", "conv_public")
        == 8
    )
    assert _optional_internal_identity(runtime, "task_internal_id", "task_public") == 7


def test_dispatch_propagates_public_conversation_identity_to_runtime_state() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "agent_runtime"
        / "api_dispatcher.py"
    ).read_text(encoding="utf-8")

    assert '"conversation_public_id": ctx.conversation_id' in source


@pytest.mark.asyncio
async def test_memory_read_kill_switch_fails_closed_before_database_access() -> None:
    from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
    from app.context_engine.models.enums import ContextKind
    from app.context_engine.sources.memory import MemorySourceAdapter

    class Resolver:
        def evaluate(self, name: str) -> bool:
            assert name == "CONTEXT_MEMORY_READ_ENABLED"
            return False

    class Runtime:
        task_flag_resolver = Resolver()
        user_internal_id = 1

        def session_factory(self):
            raise AssertionError("memory-off must not access the database")

    result = await MemorySourceAdapter().collect(
        ContextRequest(user_id="1", call_site="test.memory"),
        SectionPlan(kind=ContextKind.MEMORY, required=False, budget_tokens=100),
        ContextScope(user_id="1"),
        runtime_context=Runtime(),
    )

    assert result.items == []
    assert result.attempted is False
    assert result.degraded is False
    assert result.failure_code == "context.memory.read_disabled"


@pytest.mark.asyncio
async def test_memory_mode_uses_internal_conversation_identity() -> None:
    from app.context_engine.models.context import ContextRequest
    from app.context_engine.sources.memory import MemorySourceAdapter

    statements: list[str] = []

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return None

    class Session:
        async def execute(self, statement):
            statements.append(str(statement))
            return Result()

    @asynccontextmanager
    async def session_factory():
        yield Session()

    runtime = SimpleNamespace(session_factory=session_factory, user_internal_id=3)
    request = ContextRequest(
        user_id="3",
        conversation_id="12",
        conversation_public_id="conv_public_12",
        call_site="test.memory",
    )

    mode = await MemorySourceAdapter()._resolve_memory_mode(runtime, request)

    assert mode == "inherit"
    assert statements
    where_clause = statements[0].split("WHERE", 1)[1]
    assert "conversations.id =" in where_clause
    assert "conversations.public_id =" not in where_clause
    assert "conversations.user_id =" in where_clause


@pytest.mark.asyncio
async def test_memory_write_kill_switch_skips_classification(monkeypatch) -> None:
    from app.services.context_learning_service import ContextLearningService

    monkeypatch.setenv("CONTEXT_MEMORY_WRITE_ENABLED", "0")
    service = object.__new__(ContextLearningService)

    async def unexpected_classify(*args, **kwargs):
        raise AssertionError("memory-write-off must skip classification and persistence")

    service._classify = unexpected_classify

    result = await service.learn_from_user_message(
        user_message="请记住以后都用中文",
        user_internal_id=1,
        conversation_public_id="conv_1",
        conversation_internal_id=1,
    )

    assert result == {
        "persisted": 0,
        "error": None,
        "skipped": "memory_write_disabled",
    }
