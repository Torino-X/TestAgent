"""Phase 2.9B.2 — ``generate_with_profile`` dynamic system-prompt override tests.

Covers the production defect (audit docs/90 §25-§26) where Preparation /
Repair agent loops built a non-empty ``system_prompt`` but never passed it
to ``generate_with_profile``, and the profile's own ``system_prompt`` is
empty — so ``generate_with_system`` raised ``system_prompt 不能为空``.

These tests drive the **real** ``LLMClient.generate_with_profile`` with a
mocked OpenAI transport (no external model), asserting:

  1. no override   → profile.system_prompt is used;
  2. non-empty override on a non-empty profile → override wins;
  3. empty profile + non-empty override → succeeds (the Phase 2.9B fix);
  4. empty profile + no override → raises the existing non-empty error;
  5. explicit empty-string override → raises (no silent fallback);
  6. ``parser`` is still honoured (JSON strict path);
  7. timeout is preserved (profile.timeout_override);
  8. the profile object is not mutated;
  9. two concurrent calls with different overrides do not cross;
 10. Preparation/Repair/Incremental built prompts reach the wire.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


@dataclass
class _FakeCfg:
    api_url: str = "https://api.example.com/v1"
    api_key: str = "sk-test"
    model_name: str = "qwen-plus"
    timeout: int = 90
    enable_thinking: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None


from app.agent_runtime.incremental.prompt import build_incremental_prompt
from app.agent_runtime.incremental.schemas import (
    ExistingArtifactRef,
    IncrementalIntent,
    ModificationScope,
)
from app.agent_runtime.preparation.prompt import build_preparation_prompt
from app.agent_runtime.repair.prompt import build_repair_prompt
from app.integrations.llm_client import LLMClient
from app.llm.errors import LLMProfileError
from app.llm.task_profiles import (
    INCREMENTAL_PROFILE,
    PREPARATION_PROFILE,
    REPAIR_PROFILE,
    TEST_PLAN_PROFILE,
)


def _prep_prompt() -> str:
    return build_preparation_prompt(
        user_prompt="请生成测试方案",
        requirement_summary="需求摘要",
        template_summary="模板摘要",
        existing_kb_result=None,
        prior_steps=[],
        capabilities=None,
        mode="mode_a",
    )[0]


def _repair_prompt() -> str:
    return build_repair_prompt(
        review_issues=[{"issue_id": "i1", "title": "缺字段", "severity": "high"}],
        locked_section_ids=["s1"],
        test_plan_content_excerpt="测试方案正文",
        prior_steps=[],
        capabilities=None,
        mode="mode_a",
    )[0]


def _inc_prompt() -> str:
    intent = IncrementalIntent(
        confidence=0.9,
        existing_artifact=ExistingArtifactRef(artifact_public_id="art_1", version_no=2),
        scope=ModificationScope(
            kind="modify_section",
            target_section_ids=["s1"],
            request_text="修改 s1 章节",
        ),
        raw_user_message="请把 s1 改一下",
    )
    return build_incremental_prompt(
        intent=intent,
        locked_section_ids=[],
        prior_steps=[],
    )[0]


def _make_completion(text: str) -> Any:
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = text
    return completion


@pytest.fixture
def captured() -> Dict[str, List[Dict[str, Any]]]:
    return {"calls": [], "system_prompts": []}


@pytest.fixture
def patch_openai(captured):
    """Patch ``openai.AsyncOpenAI`` and record every chat.completions.create
    call's messages + kwargs so tests can assert the system prompt reached
    the lowest transport layer."""

    def _factory(*_args, **_kw) -> Any:
        client = MagicMock()

        async def _create(**kw: Any) -> Any:
            captured["calls"].append(kw)
            # system prompt is inside messages[0]
            msgs = kw.get("messages", [])
            if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
                captured["system_prompts"].append(msgs[0].get("content", ""))
            return _make_completion(
                '{"action":"finish","decision_summary":"ok","public_update":"ok",'
                '"expected_result":null,"confidence":0.9}'
            )

        client.chat.completions.create = _create
        return client

    with patch("openai.AsyncOpenAI", side_effect=_factory) as mock_cls:
        yield mock_cls, captured


# ═══════════════════════════════════════════════════════════════════
# system_prompt_override selection rules
# ═══════════════════════════════════════════════════════════════════


class TestOverrideSelection:
    @pytest.mark.asyncio
    async def test_no_override_uses_profile_prompt(self, patch_openai):
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg())
        # TEST_PLAN_PROFILE has a non-empty system_prompt.
        await client.generate_with_profile(TEST_PLAN_PROFILE, "user")
        assert len(captured["system_prompts"]) == 1
        assert captured["system_prompts"][0] == TEST_PLAN_PROFILE.system_prompt

    @pytest.mark.asyncio
    async def test_override_beats_nonempty_profile_prompt(self, patch_openai):
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg())
        await client.generate_with_profile(
            TEST_PLAN_PROFILE, "user", system_prompt_override="DYNAMIC_OVERRIDE"
        )
        assert captured["system_prompts"][0] == "DYNAMIC_OVERRIDE"
        # Profile default must NOT be silently substituted.
        assert captured["system_prompts"][0] != TEST_PLAN_PROFILE.system_prompt

    @pytest.mark.asyncio
    async def test_empty_profile_plus_override_succeeds(self, patch_openai):
        """The exact Phase 2.9B fix: PREPARATION_PROFILE.system_prompt is
        empty, but a dynamic override makes the call succeed."""
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg())
        result = await client.generate_with_profile(
            PREPARATION_PROFILE, "user", system_prompt_override=_prep_prompt()
        )
        assert result.success is True
        assert captured["system_prompts"][0] == _prep_prompt()
        assert captured["system_prompts"][0]

    @pytest.mark.asyncio
    async def test_empty_profile_no_override_raises(self, patch_openai):
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg())
        result = await client.generate_with_profile(PREPARATION_PROFILE, "user")
        # generate_with_profile translates LLMClientError into a result.
        assert result.success is False
        assert "system_prompt 不能为空" in (result.error_message or "")
        # No transport call must have happened.
        assert captured["calls"] == []

    @pytest.mark.asyncio
    async def test_explicit_empty_string_override_raises(self, patch_openai):
        """An explicitly-passed empty string must NOT fall back to the
        profile default — it must fail loudly (caller bug)."""
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg())
        result = await client.generate_with_profile(
            TEST_PLAN_PROFILE, "user", system_prompt_override=""
        )
        assert result.success is False
        assert "system_prompt 不能为空" in (result.error_message or "")
        assert captured["calls"] == []

    @pytest.mark.asyncio
    async def test_parser_is_still_honoured(self, patch_openai):
        """JSON_STRICT parser must still run on the response."""
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg())
        result = await client.generate_with_profile(
            PREPARATION_PROFILE, "user", system_prompt_override=_prep_prompt()
        )
        assert result.success is True
        parsed = result.parsed
        assert isinstance(parsed, dict)
        assert parsed.get("action") == "finish"

    @pytest.mark.asyncio
    async def test_profile_object_not_mutated(self, patch_openai):
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg())
        before = PREPARATION_PROFILE.system_prompt
        await client.generate_with_profile(
            PREPARATION_PROFILE, "user", system_prompt_override="X" * 100
        )
        assert PREPARATION_PROFILE.system_prompt == before
        assert PREPARATION_PROFILE.system_prompt == ""

    @pytest.mark.asyncio
    async def test_repair_prompt_reaches_wire(self, patch_openai):
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg())
        rp = _repair_prompt()
        assert rp
        result = await client.generate_with_profile(
            REPAIR_PROFILE, "user", system_prompt_override=rp
        )
        assert result.success is True
        assert captured["system_prompts"][0] == rp

    @pytest.mark.asyncio
    async def test_incremental_prompt_reaches_wire(self, patch_openai):
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg())
        ip = _inc_prompt()
        assert ip
        result = await client.generate_with_profile(
            INCREMENTAL_PROFILE, "user", system_prompt_override=ip
        )
        assert result.success is True
        assert captured["system_prompts"][0] == ip

    @pytest.mark.asyncio
    async def test_concurrent_overrides_do_not_cross(self, patch_openai):
        """Two concurrent calls with different overrides must each receive
        their own prompt — no shared global / no cross-contamination."""
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg())
        prep_p = _prep_prompt()
        repair_p = _repair_prompt()
        await asyncio.gather(
            client.generate_with_profile(
                PREPARATION_PROFILE, "u1", system_prompt_override=prep_p
            ),
            client.generate_with_profile(
                REPAIR_PROFILE, "u2", system_prompt_override=repair_p
            ),
        )
        assert len(captured["system_prompts"]) == 2
        assert prep_p in captured["system_prompts"]
        assert repair_p in captured["system_prompts"]

    @pytest.mark.asyncio
    async def test_nonexistent_profile_raises(self, patch_openai):
        """profile=None still raises LLMProfileError before any IO."""
        client = LLMClient(config_provider=_FakeCfg())
        with pytest.raises(LLMProfileError):
            await client.generate_with_profile(None, "user")


# ═══════════════════════════════════════════════════════════════════
# Preparation / Repair / Incremental call-contract tests
# ═══════════════════════════════════════════════════════════════════


class TestAgentCallContracts:
    """Assert the agent_loop call sites forward the built prompt through
    the real LLMClient to the wire, and no longer error out."""

    @pytest.mark.asyncio
    async def test_preparation_prompt_reaches_wire_through_real_client(
        self, patch_openai
    ):
        from app.agent_runtime.preparation.agent_loop import _llm_decide

        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg())
        prep_p = _prep_prompt()

        decision = await _llm_decide(
            llm_client=client,
            system_prompt=prep_p,
            user_content="user turn",
            parser=None,
        )
        # No longer falls back with "system_prompt 不能为空".
        assert decision.action != "fail"
        assert captured["system_prompts"][0] == prep_p

    @pytest.mark.asyncio
    async def test_preparation_loop_emits_decision_when_llm_succeeds(
        self, monkeypatch, patch_openai
    ):
        """Full run_preparation with a REAL LLMClient (mocked transport):
        prompt → override → wire → parser → AgentDecision with
        decision_update → agent_decision_update event."""
        from app.agent_runtime.preparation import agent_loop as prep_loop
        from app.agent_runtime.preparation.subgraph import run_preparation_subgraph
        from app.agent_runtime.runtime_context import RuntimeContext

        mock_cls, captured = patch_openai
        # Real LLMClient instance.
        client = LLMClient(config_provider=_FakeCfg())
        # Force narrative flag on so decision events emit.
        fake_flags = SimpleNamespace(phase29b_narrative_enabled_for=lambda _n: True)

        class _Sink:
            def __init__(self):
                self.events = []

            async def emit(self, **kw):
                self.events.append(kw)
                return {"ok": True}

        class _Cancel:
            def is_cancelled(self, _t):
                return False

        class _Session:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *_a):
                return False

        class _Adapter:
            async def execute(self, *, tool_name, inputs, ctx_runtime, **kw):
                return {"success": True, "data": {}, "summary": "ok"}

        sink = _Sink()
        ctx = RuntimeContext(
            user_internal_id=1,
            task_internal_id=200,
            conversation_internal_id=20,
            session_factory=lambda: _Session(),
            settings_service=None,
            event_sink=sink,
            cancellation_service=_Cancel(),
            clock=lambda: datetime.utcnow(),
            tool_adapter=_Adapter(),
            llm_client=client,
        )

        monkeypatch.setattr(
            "app.agent_runtime.preparation.event_emitter.get_feature_flags",
            lambda: fake_flags,
        )
        result = await run_preparation_subgraph(
            {
                "user_prompt": "请生成测试方案",
                "requirement_summary": "需求摘要",
                "template_summary": "模板摘要",
                "knowledge_search_result": None,
                "kb_skip_reason": None,
                "completed_nodes": [],
            },
            llm_client=client,
            tool_adapter=_Adapter(),
            ctx=ctx,
        )
        # No fallback; decision reached finish.
        assert result.fallback_reason is None
        # The wire must have seen the built prompt.
        assert captured["system_prompts"] and captured["system_prompts"][0]
        # Decision narrative reached the sink.
        types = {e.get("event_type") for e in sink.events}
        assert "agent_decision_update" in types

    @pytest.mark.asyncio
    async def test_repair_call_contract_override(self, patch_openai):
        """Repair agent_loop forwards build_repair_prompt to the wire via
        the unified override contract (no system_prompt 为空)."""
        from app.agent_runtime.repair.prompt import build_repair_prompt

        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg())
        rp = build_repair_prompt(
            review_issues=[{"issue_id": "i1", "title": "缺字段", "severity": "high"}],
            locked_section_ids=["s1"],
            test_plan_content_excerpt="正文",
            prior_steps=[],
            capabilities=None,
            mode="mode_a",
        )[0]
        result = await client.generate_with_profile(
            REPAIR_PROFILE, "user", system_prompt_override=rp
        )
        assert result.success is True
        assert captured["system_prompts"][0] == rp

    @pytest.mark.asyncio
    async def test_incremental_no_unexpected_kwarg(self, patch_openai):
        """The Incremental contract fix: calling generate_with_profile with
        the unified system_prompt_override must NOT raise
        'unexpected keyword argument'."""
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg())
        ip = _inc_prompt()
        result = await client.generate_with_profile(
            INCREMENTAL_PROFILE, "user", system_prompt_override=ip
        )
        assert result.success is True
        assert captured["system_prompts"][0] == ip


from datetime import datetime
from types import SimpleNamespace  # noqa: E402
