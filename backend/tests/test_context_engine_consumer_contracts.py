"""L4 contracts: composed Context Engine output reaches the final LLM boundary.

These tests intentionally use the real planner, source orchestration,
selection, composer and ContextAwareLLMInvoker.  Only the external provider,
snapshot persistence and source repositories are deterministic fakes.  They
therefore protect against a Bridge that exists but fails to pass composed
context to the final LLM client.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent_runtime.context.llm_invoker import ContextAwareLLMInvoker
from app.context_engine.models.context import ContextItem, ContextRequest
from app.context_engine.models.enums import ContextKind, ContextTrust
from app.context_engine.models.snapshot_models import ContextSnapshotRef
from app.context_engine.models.source import SourceCollectResult
from app.context_engine.runtime import build_context_engine
from app.context_engine.sources.registry import SourceAdapterRegistry


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def commit(self):
        return None


class _SnapshotWriter:
    def __init__(self):
        self._next = 0
        self.states: list[str] = []

    async def begin_build(self, _command, *, session):
        self._next += 1
        self.states.append("building")
        return ContextSnapshotRef(public_id=f"cs_l4_{self._next}", status="building")

    async def mark_ready(self, *, session, public_id, user_id):
        self.states.append("ready")

    async def mark_sent(self, *, session, public_id, user_id):
        self.states.append("sent")

    async def complete(self, command, *, session, user_id):
        self.states.append("completed")

    async def fail(self, command, *, session, user_id):
        self.states.append("failed")

    async def abandon(self, *, session, public_id, user_id):
        self.states.append("abandoned")


class _CapturingLLM:
    def __init__(self):
        self.calls: list[dict[str, str | None]] = []

    async def generate_with_profile(self, profile, user_content, *, system_prompt_override=None):
        self.calls.append(
            {
                "user_content": user_content,
                "system_prompt": system_prompt_override,
            }
        )
        return SimpleNamespace(
            success=True,
            parsed={"ok": True},
            usage={"input": 101, "output": 17},
            provider_request_id="fake-provider-request",
        )


class _DeterministicSource:
    """Scope-aware source fake used only below the real CE composition path."""

    def __init__(self, kind: ContextKind):
        self.source_kind = kind

    async def collect(self, request, section_plan, scope, *, runtime_context):
        source_type = (section_plan.source_types or ["conversation"])[0]
        content = f"L4_{self.source_kind.value.upper()}"
        trust = ContextTrust.UNTRUSTED_REFERENCE
        authority = 50
        if self.source_kind in {ContextKind.SYSTEM_RULES, ContextKind.CALL_CONTRACT}:
            content = f"L4_TRUSTED_{self.source_kind.value.upper()}"
            trust = ContextTrust.TRUSTED_INSTRUCTION
            authority = 100
        elif self.source_kind == ContextKind.CURRENT_GOAL:
            content = request.current_user_message or "L4_CURRENT_GOAL"
            trust = ContextTrust.TRUSTED_INSTRUCTION
            authority = 100
        elif self.source_kind == ContextKind.PROJECT_INSTRUCTIONS:
            content = (
                "PROJECT_A_INSTRUCTION"
                if scope.workspace_key == "project:alpha"
                else "PROJECT_B_INSTRUCTION"
            )
            trust = ContextTrust.TRUSTED_INSTRUCTION
            authority = 100
        elif self.source_kind == ContextKind.MEMORY:
            # The provider-facing contract sees only the current ACTIVE value;
            # candidate/rejected/forgotten and superseded v1 never enter it.
            content = "MEMORY_V2_ACTIVE_ONLY"
        elif self.source_kind == ContextKind.KNOWLEDGE:
            content = "DETERMINISTIC_KNOWLEDGE_HIT"
        elif self.source_kind == ContextKind.CONVERSATION:
            # The compact summary is deliberately the sole conversation item.
            content = "COMPACTED_CONVERSATION_V2"

        return SourceCollectResult(
            adapter_key=f"l4.{self.source_kind.value}",
            kind=self.source_kind,
            items=[
                ContextItem(
                    item_id=f"l4_{self.source_kind.value}",
                    kind=self.source_kind,
                    source_type=source_type,
                    source_ref=f"l4:{self.source_kind.value}",
                    content=content,
                    authority=authority,
                    estimated_tokens=12,
                    trust=trust,
                )
            ],
        )


def _make_invoker() -> tuple[ContextAwareLLMInvoker, _CapturingLLM, _SnapshotWriter]:
    registry = SourceAdapterRegistry()
    for kind in ContextKind:
        registry.register(_DeterministicSource(kind))
    writer = _SnapshotWriter()
    engine = build_context_engine(source_registry=registry, snapshot_writer=writer)
    llm = _CapturingLLM()
    invoker = ContextAwareLLMInvoker(
        engine=engine,
        snapshot_writer=writer,
        llm_client_factory=lambda _runtime: llm,
    )
    return invoker, llm, writer


def _runtime_context():
    return SimpleNamespace(
        user_internal_id=1,
        conversation_internal_id=2,
        task_internal_id=3,
        session_factory=lambda: _Session(),
        settings_service=None,
        cancellation_service=None,
    )


def _profile():
    return SimpleNamespace(system_prompt="BASE_PROFILE_SYSTEM", parser="json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_site",
    [
        "chat.reply",
        "test_plan.generate.outline",
        "test_plan.review",
        "test_plan.repair.plan",
    ],
)
async def test_real_context_engine_composition_reaches_final_llm_for_core_calls(call_site):
    invoker, llm, writer = _make_invoker()
    marker = f"FINAL_CONSUMER_{call_site}"

    result = await invoker.invoke(
        request=ContextRequest(
            user_id="1",
            conversation_id="2",
            task_id="3",
            call_site=call_site,
            current_user_message=marker,
            model_context_window=128_000,
        ),
        llm_task_profile=_profile(),
        runtime_context=_runtime_context(),
    )

    assert result.value == {"ok": True}
    assert len(llm.calls) == 1
    assert marker in str(llm.calls[0]["user_content"])
    assert "L4_TRUSTED_SYSTEM_RULES" in str(llm.calls[0]["system_prompt"])
    assert writer.states == ["building", "ready", "sent", "completed"]


@pytest.mark.asyncio
async def test_project_memory_knowledge_and_compaction_contracts_reach_final_llm_without_cross_scope_data():
    invoker, llm, _writer = _make_invoker()

    await invoker.invoke(
        request=ContextRequest(
            user_id="1",
            conversation_id="2",
            task_id="3",
            call_site="chat.reply",
            current_user_message="PROJECT_CONSUMER_GOAL",
            workspace_key="project:alpha",
            model_context_window=128_000,
        ),
        llm_task_profile=_profile(),
        runtime_context=_runtime_context(),
    )

    prompt = str(llm.calls[0]["user_content"])
    assert "PROJECT_A_INSTRUCTION" in prompt
    assert "PROJECT_B_INSTRUCTION" not in prompt
    assert "MEMORY_V2_ACTIVE_ONLY" in prompt
    assert "DETERMINISTIC_KNOWLEDGE_HIT" in prompt
    assert "COMPACTED_CONVERSATION_V2" in prompt
    assert "MEMORY_V1" not in prompt
    assert "CANDIDATE" not in prompt
    assert "REJECTED" not in prompt
    assert "FORGOTTEN" not in prompt
    assert "RAW_PRE_COMPACTION_SECRET" not in prompt
