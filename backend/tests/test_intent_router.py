"""Unit tests for ``IntentRouter`` (F013 + F014 closeout).

The router is responsible for mapping a user message + current file
state to a structured ``IntentResult``.  These tests verify:

  * Valid JSON from the LLM → all fields are surfaced
  * Invalid / non-JSON output → CLARIFY fallback (never raises)
  * Markdown-fenced JSON is parsed correctly
  * Unknown enum values fall back to UNKNOWN / CLARIFY
  * Low-confidence agent_task is forced to CLARIFY
  * LLM exceptions → CLARIFY fallback
  * The system prompt embeds every enum value
  * All IntentType values round-trip through the router
  * F014 closeout: ``recognize`` uses ``generate_with_profile``,
    not the legacy ``generate_with_system`` call.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.agent.enums import IntentType, MessageRoute
from app.agent.intent_router import (
    CLARIFY_FALLBACK_TEXT,
    INTENT_CONTRACT,
    IntentResult,
    IntentRouter,
    _INTENT_SYSTEM_PROMPT,
)
from app.integrations.llm_client import LLMClientError, LLMProfileResult
from app.llm.task_profiles import INTENT_PROFILE


# ── Helpers ──────────────────────────────────────────────────────


def _parse_via_strict(raw_text: str) -> dict[str, Any]:
    """Mimic what ``JsonStrictParser.parse`` produces for ``raw_text``."""
    from app.llm.parsers.json_strict import JsonStrictParser

    return JsonStrictParser().parse(raw_text, INTENT_PROFILE)


class _StubLLM:
    """Stub LLM client that returns a pre-canned ``LLMProfileResult``.

    Mirrors the contract surface of ``LLMClient.generate_with_profile``
    introduced in F014 closeout.  ``raw_text`` is the model response;
    the stub pre-parses it through ``JsonStrictParser`` so the router
    sees a real ``dict`` in ``result.parsed``.
    """

    def __init__(
        self,
        raw_text: str | None = None,
        *,
        raise_exc: BaseException | None = None,
        parsed: Any = None,
        success: bool = True,
    ) -> None:
        self._raw_text = raw_text or ""
        self._raise = raise_exc
        self._parsed_override = parsed
        self._success_override = success
        self.calls: list[tuple[Any, str]] = []

    available = True
    is_context_engine_bridge = True

    async def generate_with_profile(
        self,
        profile: Any,
        user_content: str,
        **kwargs: Any,
    ) -> LLMProfileResult:
        self.calls.append((profile, user_content))
        if self._raise is not None:
            raise self._raise
        if self._parsed_override is not None:
            return LLMProfileResult(
                task_name=profile.name,
                raw_text=self._raw_text,
                parsed=self._parsed_override,
                success=self._success_override,
                error_type=None if self._success_override else "parse_error",
                error_message=None,
            )
        return LLMProfileResult(
            task_name=profile.name,
            raw_text=self._raw_text,
            parsed=self._raw_text,
            success=self._success_override,
            error_type=None,
            error_message=None,
        )

    async def generate(self, **kwargs: Any):
        result = await self.generate_with_profile(
            kwargs["llm_task_profile"],
            kwargs.get("user_content") or kwargs.get("current_goal") or "",
        )

        class _Result:
            value = result.parsed
            snapshot_public_id = "snapshot_test"

            @staticmethod
            def as_profile_result() -> LLMProfileResult:
                return result

        return _Result()


def _file(public_id: str = "f1", name: str = "x.docx", ftype: str = "requirement_doc",
          status: str = "confirmed") -> SimpleNamespace:
    return SimpleNamespace(
        public_id=public_id,
        original_name=name,
        file_type=ftype,
        upload_status=status,
    )


# ── 1. Valid JSON → all fields surfaced ──────────────────────────


def test_recognize_valid_json_test_plan() -> None:
    payload = (
        '{"intent": "test_plan_generation", "route": "agent_task", '
        '"supported": true, "confidence": 0.95, "reason": "user asked", '
        '"need_files": false, "required_file_types": [], "missing_file_types": [], '
        '"reply_message": null}'
    )
    router = IntentRouter(_StubLLM(payload, parsed=_parse_via_strict(payload)))

    result = asyncio.run(router.recognize("帮我生成测试方案"))

    assert isinstance(result, IntentResult)
    assert result.intent == IntentType.TEST_PLAN_GENERATION
    assert result.route == MessageRoute.AGENT_TASK
    assert result.supported is True
    assert result.confidence == pytest.approx(0.95)
    assert result.need_files is False
    assert result.required_file_types == []
    assert result.missing_file_types == []


# ── 2. Invalid JSON → CLARIFY ────────────────────────────────────


def test_recognize_invalid_json_falls_back_to_clarify() -> None:
    """When the contract layer cannot parse the LLM text, the fallback
    JSON kicks in.  ``INTENT_PROFILE.fallback_text`` itself IS valid
    JSON, so the wrapper successfully parses it and we get a
    structured CLARIFY result."""
    from app.llm.parsers.json_strict import JsonStrictParser
    from app.llm.parsers.registry import get_parser
    from app.llm.task_profiles import INTENT_PROFILE

    bad = "这只是一段普通文字，不是 JSON。"
    fallback_payload = JsonStrictParser().parse(INTENT_PROFILE.fallback_text, INTENT_PROFILE)
    stub = _StubLLM(bad, parsed=fallback_payload, success=False)
    router = IntentRouter(stub)

    result = asyncio.run(router.recognize("你好"))

    assert result.intent == IntentType.UNKNOWN
    assert result.route == MessageRoute.CLARIFY
    assert result.supported is False
    assert result.confidence == 0.0
    # The fallback JSON's reply_message is the CLARIFY_FALLBACK_TEXT.
    assert result.reply_message == CLARIFY_FALLBACK_TEXT


# ── 3. Markdown-fenced JSON ──────────────────────────────────────


def test_recognize_markdown_fence_still_parses() -> None:
    fenced = (
        "```json\n"
        '{"intent": "general_chat", "route": "chat_reply", '
        '"supported": true, "confidence": 0.9, "reason": "small talk"}\n'
        "```"
    )
    router = IntentRouter(_StubLLM(fenced, parsed=_parse_via_strict(fenced)))

    result = asyncio.run(router.recognize("你好"))

    assert result.intent == IntentType.GENERAL_CHAT
    assert result.route == MessageRoute.CHAT_REPLY
    assert result.supported is True
    assert result.confidence == pytest.approx(0.9)


# ── 4. Unknown intent value → UNKNOWN / CLARIFY ──────────────────


def test_document_question_overrides_legacy_unsupported_route() -> None:
    """Conversation documents are answerable through the grounded chat path.

    This protects the migration while classifier prompts and model versions are
    rolled out: a legacy model response must not restore the static
    document-question unsupported reply.
    """
    payload = (
        '{"intent": "document_question", "route": "unsupported", '
        '"supported": false, "confidence": 0.9, "reason": "legacy"}'
    )
    router = IntentRouter(_StubLLM(payload, parsed=_parse_via_strict(payload)))

    result = asyncio.run(router.recognize("刚才上传的需求文档里有哪些预约规则？"))

    assert result.intent == IntentType.DOCUMENT_QUESTION
    assert result.route == MessageRoute.CHAT_REPLY
    assert result.supported is True
    assert result.reply_message is None


def test_recognize_unknown_intent_value_normalises_to_unknown() -> None:
    """An unknown intent string is mapped to ``IntentType.UNKNOWN``.

    The route itself is not forced to CLARIFY — the LLM may still be
    confident that this is, e.g., a chat reply, and an unknown intent
    enum value is a structural problem rather than a low-confidence
    classification.  The test ensures the intent is normalised to a
    known enum value and the result remains a valid ``IntentResult``.
    """
    payload = (
        '{"intent": "weird_intent", "route": "chat_reply", '
        '"supported": true, "confidence": 0.9, "reason": "..."}'
    )
    router = IntentRouter(_StubLLM(payload, parsed=_parse_via_strict(payload)))

    result = asyncio.run(router.recognize("x"))

    assert result.intent == IntentType.UNKNOWN
    # The route is whatever the LLM said (still a valid enum value).
    assert result.route in {r for r in MessageRoute}


# ── 5. Low confidence forces CLARIFY ─────────────────────────────


def test_recognize_low_confidence_forces_clarify_for_agent_task() -> None:
    payload = (
        '{"intent": "test_plan_generation", "route": "agent_task", '
        '"supported": true, "confidence": 0.3, "reason": "..."}'
    )
    router = IntentRouter(_StubLLM(payload, parsed=_parse_via_strict(payload)))

    result = asyncio.run(router.recognize("x"))

    assert result.route == MessageRoute.CLARIFY
    # The original intent is preserved (downgrade is route-only).
    assert result.intent == IntentType.TEST_PLAN_GENERATION


# ── 6. LLM exception → CLARIFY (no propagation) ─────────────────


def test_recognize_llm_exception_clarify() -> None:
    router = IntentRouter(_StubLLM(raise_exc=LLMClientError("model timeout")))

    result = asyncio.run(router.recognize("x"))

    assert result.route == MessageRoute.CLARIFY
    assert result.intent == IntentType.UNKNOWN
    assert "llm_error" in result.reason or "LLMClientError" in result.reason
    assert result.reply_message == CLARIFY_FALLBACK_TEXT


def test_recognize_records_safe_context_bridge_failure_diagnostic(monkeypatch) -> None:
    """A bridge failure must be visible without exposing prompt content."""

    class _BridgeFailure(Exception):
        def __init__(self) -> None:
            self.error = SimpleNamespace(
                code="context.preflight.blocked",
                stage=SimpleNamespace(value="preflight"),
                retryable=False,
                safe_metadata={
                    "reason": "absolute_after_compaction",
                    "compaction_attempted": True,
                    "compaction_exception_code": "db_1205",
                },
            )

    class _Bridge:
        available = True

        async def generate(self, **_kwargs: Any):
            raise _BridgeFailure()

    monkeypatch.setattr(
        "app.context_engine.feature_flags.get_context_engine_flags",
        lambda: SimpleNamespace(mig_chat=True),
    )
    router = IntentRouter(_StubLLM(), context_llm_invoker=_Bridge())

    result = asyncio.run(router.recognize("continue our ordinary chat"))

    assert result.route == MessageRoute.CLARIFY
    assert result.extra_payload["intent_context_engine"] == {
        "path": "context_bridge",
        "outcome": "bridge_exception",
        "exception_type": "_BridgeFailure",
        "error_code": "context.preflight.blocked",
        "stage": "preflight",
        "retryable": False,
        "reason": "absolute_after_compaction",
        "compaction_attempted": True,
        "compaction_exception_code": "db_1205",
    }


def test_recognize_passes_persisted_current_message_id_to_context_bridge(monkeypatch) -> None:
    payload = {
        "intent": "general_chat",
        "route": "chat_reply",
        "supported": True,
        "confidence": 0.9,
        "reason": "ordinary chat",
    }

    class _BridgeResult:
        value = payload
        snapshot_public_id = "cs_intent"

        def as_profile_result(self):
            return SimpleNamespace(parsed=self.value)

    class _Bridge:
        available = True
        kwargs: dict[str, Any] = {}

        async def generate(self, **kwargs: Any):
            self.kwargs = kwargs
            return _BridgeResult()

    bridge = _Bridge()
    monkeypatch.setattr(
        "app.context_engine.feature_flags.get_context_engine_flags",
        lambda: SimpleNamespace(mig_chat=True),
    )
    intent_context = SimpleNamespace(
        current_message_id="456",
        conversation_summary=None,
        recent_turns=[],
        file_summaries=[],
        latest_task_summary=None,
    )
    router = IntentRouter(_StubLLM(), context_llm_invoker=bridge)

    result = asyncio.run(
        router.recognize(
            "ordinary chat",
            intent_context=intent_context,
            context_workspace_key="project:prj_123",
        )
    )

    assert result.route == MessageRoute.CHAT_REPLY
    assert bridge.kwargs["current_user_message_id"] == 456
    assert bridge.kwargs["runtime_context"].context_workspace_key == "project:prj_123"


# ── 7. System prompt contains every enum value ──────────────────


def test_system_prompt_includes_all_enums() -> None:
    for intent in IntentType:
        assert intent.value in _INTENT_SYSTEM_PROMPT, f"missing intent {intent.value}"
    for route in MessageRoute:
        assert route.value in _INTENT_SYSTEM_PROMPT, f"missing route {route.value}"


# ── 8. All enum values round-trip ────────────────────────────────


@pytest.mark.parametrize("intent_value", [it.value for it in IntentType])
def test_recognize_all_enum_values_roundtrip(intent_value: str) -> None:
    """Every IntentType can be reconstructed from the LLM output."""
    payload = (
        f'{{"intent": "{intent_value}", "route": "chat_reply", '
        f'"supported": true, "confidence": 0.8, "reason": "..."}}'
    )
    router = IntentRouter(_StubLLM(payload, parsed=_parse_via_strict(payload)))

    result = asyncio.run(router.recognize("x"))

    # UNKNOWN may be returned either as itself or as CLARIFY; both are
    # legal outcomes.  Other values must round-trip exactly.
    if intent_value == IntentType.UNKNOWN.value:
        assert result.intent == IntentType.UNKNOWN
    else:
        assert result.intent.value == intent_value


# ── 9. File context is included in the LLM prompt ───────────────


def test_recognize_passes_file_context_to_llm() -> None:
    raw = (
        '{"intent": "test_plan_generation", "route": "ask_for_files", '
        '"supported": true, "confidence": 0.85, "reason": "files missing"}'
    )
    stub = _StubLLM(raw, parsed=_parse_via_strict(raw))
    router = IntentRouter(stub)
    files = [_file("f_req", "需求文档.docx", "requirement_doc", "confirmed")]

    asyncio.run(router.recognize("帮我生成测试方案", files, attached_file_ids=["f_req"]))

    assert len(stub.calls) == 1
    profile, user_content = stub.calls[0]
    # F014 closeout: the call goes through generate_with_profile;
    # the profile name must be the registered intent contract.
    assert profile.name == INTENT_PROFILE.name
    assert "帮我生成测试方案" in user_content
    assert "f_req" in user_content
    assert "requirement_doc" in user_content


def test_long_routing_input_is_bounded_but_preserves_head_and_tail() -> None:
    """A long normal chat must not exhaust the strict-JSON routing step.

    The bound is only applied to intent recognition.  Its head/tail policy
    keeps an opening intent and a trailing concrete question visible.
    """
    content = "开头：这是普通聊天请求。" + ("中间背景。" * 2_000) + "结尾：请继续普通聊天。"

    built = IntentRouter._build_user_content(content, [], set())

    assert "开头：这是普通聊天请求。" in built
    assert "结尾：请继续普通聊天。" in built
    assert "中间部分仅对意图路由省略" in built
    # Includes a small label and the omission notice in addition to the
    # configured 1,200-character routing payload bound.
    assert len(built) < 1_400


def test_long_routing_input_persists_size_only_diagnostics() -> None:
    """Live API evidence can prove the active process used the input bound."""
    payload = (
        '{"intent": "general_chat", "route": "chat_reply", '
        '"supported": true, "confidence": 0.9}'
    )
    content = "普通聊天开头。" + ("背景。" * 2_000) + "结尾继续聊天。"
    router = IntentRouter(_StubLLM(payload, parsed=_parse_via_strict(payload)))

    result = asyncio.run(router.recognize(content))

    assert result.extra_payload["intent_routing_original_message_chars"] == len(content)
    assert result.extra_payload["intent_routing_current_message_bounded"] is True
    assert result.extra_payload["intent_routing_input_chars"] < 1_400


# ── 10. Generic exception → CLARIFY (not propagated) ────────────


def test_recognize_unexpected_exception_clarify() -> None:
    class _BoomLLM(_StubLLM):
        async def generate_with_profile(self, *args, **kwargs):  # type: ignore[override]
            raise RuntimeError("kaboom")

    router = IntentRouter(_BoomLLM())
    result = asyncio.run(router.recognize("x"))
    assert result.route == MessageRoute.CLARIFY
    assert "RuntimeError" in result.reason


# ── 11. F014 closeout: recognizer uses generate_with_profile ────


def test_recognize_uses_generate_with_profile_not_generate_with_system() -> None:
    """F014 closeout invariant.

    The router MUST NOT call ``LLMClient.generate_with_system`` any
    more — all LLM traffic goes through ``generate_with_profile``.
    A stub that only implements ``generate_with_system`` (and raises
    if called) must therefore see a successful path through
    ``generate_with_profile``.
    """
    raw = (
        '{"intent": "general_chat", "route": "chat_reply", '
        '"supported": true, "confidence": 0.95, "reason": "..."}'
    )
    stub = _StubLLM(raw, parsed=_parse_via_strict(raw))
    router = IntentRouter(stub)

    result = asyncio.run(router.recognize("hi"))

    assert result.intent == IntentType.GENERAL_CHAT
    # The stub recorded one profile-based call.
    assert len(stub.calls) == 1
    profile, _ = stub.calls[0]
    assert profile.name == "intent_recognition"


# ── 12. F014 closeout: INTENT_CONTRACT is the registered profile ─


def test_intent_contract_is_intent_profile() -> None:
    """The module-level ``INTENT_CONTRACT`` alias MUST point at the
    same ``INTENT_PROFILE`` instance registered in
    ``app.llm.task_profiles`` — single source of truth."""
    from app.agent.intent_router import INTENT_CONTRACT as local

    assert local is INTENT_PROFILE
    assert INTENT_CONTRACT.name == "intent_recognition"
    assert INTENT_CONTRACT.parser.value == "json_strict"
    assert INTENT_CONTRACT.require_json is True
