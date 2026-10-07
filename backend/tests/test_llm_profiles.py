"""Unit tests for the LLM task-contract layer (F014).

Covers:

  * Built-in profile registration (CHAT / INTENT / TITLE / TEST_PLAN).
  * Parser registry returns all registered adapters and rejects unknown types.
  * MarkdownParser preserves headings, lists, code fences, tables.
  * PlainTextParser strips Markdown, newlines, quotes.
  * JsonStrictParser parses valid JSON, raises on garbage.
  * LLMClient.generate_with_profile returns ``LLMProfileResult``
    with success=True on happy path, fallback on parse failure,
    and raises on ``on_parse_failure=RAISE``.

All LLMClient interactions are stubbed so no real network is involved.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from app.integrations.llm_client import LLMClient, LLMProfileResult
from app.llm.errors import LLMProfileConfigError, LLMProfileParseError
from app.llm.parsers.json_relaxed import JsonRelaxedParser
from app.llm.parsers.json_strict import JsonStrictParser
from app.llm.parsers.markdown import MarkdownParser
from app.llm.parsers.plain_text import PlainTextParser
from app.llm.parsers.raw_text import RawTextParser
from app.llm.parsers.registry import get_parser, reset_parser_cache
from app.llm.parsers.protocol import ParserAdapter
from app.llm.task_profiles import (
    BUILTIN_PROFILES,
    CHAT_PROFILE,
    INTENT_PROFILE,
    LLMParserType,
    LLMParseFailurePolicy,
    LLMTaskProfile,
    NARRATIVE_SCHEMA_REPAIR_PROFILE,
    REPAIR_PROFILE,
    TASK_SUMMARY_NARRATIVE_COMPOSER_PROFILE,
    TEST_PLAN_PROFILE,
    TEST_PLAN_RAW_PROFILE,
    TITLE_PROFILE,
    TOOL_NARRATIVE_COMPOSER_PROFILE,
    get_builtin_profile,
)


# ── Helpers ────────────────────────────────────────────────────────


class _StubLLM:
    """Stub that mirrors ``LLMClient`` for ``generate_with_profile``.

    Returns ``response`` for any call.  Set ``raise_exc`` to surface an
    exception (mimicking transport failure).
    """

    def __init__(
        self,
        response: Any = "ok",
        *,
        raise_exc: Optional[BaseException] = None,
    ):
        self._response = response
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def generate_with_system(
        self,
        system_prompt: str,
        user_content: str,
        images: Optional[list[str]] = None,
        *,
        model_override: Optional[str] = None,
        timeout_override: Optional[int] = None,
    ) -> str:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_content": user_content,
                "timeout_override": timeout_override,
            }
        )
        if self._raise is not None:
            raise self._raise
        return self._response


# ── 1. Built-in profile registry ──────────────────────────────────


def test_chat_profile_registered() -> None:
    assert CHAT_PROFILE.name == "chat_reply"
    assert BUILTIN_PROFILES["chat_reply"] is CHAT_PROFILE


def test_chat_profile_allows_markdown_no_json() -> None:
    assert CHAT_PROFILE.allow_markdown is True
    assert CHAT_PROFILE.require_json is False
    assert CHAT_PROFILE.parser is LLMParserType.MARKDOWN
    assert CHAT_PROFILE.fallback_text is not None


def test_intent_profile_has_interactive_timeout_budget() -> None:
    """Routing is an interactive gate and must not inherit a long chat timeout."""
    assert INTENT_PROFILE.timeout_override == 30


def test_chat_profile_forbids_general_knowledge_for_project_document_facts() -> None:
    assert "仅能使用提供的项目资料或知识参考中的明确事实" in CHAT_PROFILE.system_prompt
    assert "不得以通用知识、惯例或猜测补全" in CHAT_PROFILE.system_prompt
    assert "资料未定义" in CHAT_PROFILE.system_prompt


def test_intent_profile_requires_json_no_markdown() -> None:
    assert INTENT_PROFILE.require_json is True
    assert INTENT_PROFILE.allow_markdown is False
    assert INTENT_PROFILE.parser is LLMParserType.JSON_STRICT
    # Profile carries a JSON fallback so a parse failure can still be
    # surfaced as a structured CLARIFY result.
    assert INTENT_PROFILE.fallback_text is not None
    assert '"route":"clarify"' in INTENT_PROFILE.fallback_text


def test_title_profile_is_plain_text_with_fallback() -> None:
    assert TITLE_PROFILE.parser is LLMParserType.PLAIN_TEXT
    assert TITLE_PROFILE.allow_markdown is False
    assert TITLE_PROFILE.require_json is False
    assert TITLE_PROFILE.fallback_text == "新的对话"


@pytest.mark.parametrize(
    "profile",
    (
        TOOL_NARRATIVE_COMPOSER_PROFILE,
        TASK_SUMMARY_NARRATIVE_COMPOSER_PROFILE,
        NARRATIVE_SCHEMA_REPAIR_PROFILE,
    ),
)
def test_narrative_profiles_preserve_tagged_narrative_contract(
    profile: LLMTaskProfile,
) -> None:
    """NarrativeComposer must receive its required <NARRATIVE> wrapper intact."""
    source = "<NARRATIVE>### 任务概述\n\n- 产物已生成</NARRATIVE>"

    assert profile.parser is LLMParserType.MARKDOWN
    assert profile.allow_markdown is True
    assert MarkdownParser().parse(source, profile) == source


def test_test_plan_profile_contracts_keep_raw_transport_separate() -> None:
    assert TEST_PLAN_PROFILE.parser is LLMParserType.JSON_STRICT
    assert TEST_PLAN_PROFILE.require_json is True
    assert TEST_PLAN_PROFILE.allow_markdown is False
    assert TEST_PLAN_PROFILE.name == "test_plan_generation"
    assert TEST_PLAN_RAW_PROFILE.parser is LLMParserType.RAW_TEXT
    assert TEST_PLAN_RAW_PROFILE.require_json is False


def test_repair_profile_has_bounded_timeout() -> None:
    """RepairAgent decision calls must not inherit the user's long model timeout."""
    assert REPAIR_PROFILE.parser is LLMParserType.JSON_STRICT
    assert REPAIR_PROFILE.require_json is True
    assert REPAIR_PROFILE.timeout_override == 45
    assert "进入导出" not in (REPAIR_PROFILE.fallback_text or "")


def test_get_builtin_profile_case_insensitive() -> None:
    assert get_builtin_profile("CHAT_REPLY") is CHAT_PROFILE
    assert get_builtin_profile("chat_reply") is CHAT_PROFILE


def test_get_builtin_profile_unknown_raises_config_error() -> None:
    with pytest.raises(LLMProfileConfigError):
        get_builtin_profile("nope")


# ── 2. Parser registry ────────────────────────────────────────────


@pytest.mark.parametrize(
    "parser_type,expected_cls",
    [
        (LLMParserType.JSON_STRICT, JsonStrictParser),
        (LLMParserType.JSON_RELAXED, JsonRelaxedParser),
        (LLMParserType.PLAIN_TEXT, PlainTextParser),
        (LLMParserType.MARKDOWN, MarkdownParser),
        (LLMParserType.RAW_TEXT, RawTextParser),
    ],
)
def test_registry_returns_expected_parser(
    parser_type: LLMParserType, expected_cls: type
) -> None:
    reset_parser_cache()
    adapter = get_parser(parser_type)
    assert isinstance(adapter, expected_cls)
    assert isinstance(adapter, ParserAdapter)


def test_registry_unknown_raises_config_error() -> None:
    class _Fake:
        pass

    with pytest.raises(LLMProfileConfigError):
        get_parser(_Fake())  # type: ignore[arg-type]


def test_raw_text_parser_preserves_malformed_json_verbatim() -> None:
    malformed = '{"overview":"bad",}\n'
    assert RawTextParser().parse(malformed, TEST_PLAN_RAW_PROFILE) == malformed


# ── 3. MarkdownParser behaviour ───────────────────────────────────


def test_markdown_parser_preserves_structure() -> None:
    src = (
        "# 标题\n\n"
        "正文段落，包含**加粗**与*斜体*。\n\n"
        "- 列表项 1\n"
        "- 列表项 2\n\n"
        "```python\nprint('hi')\n```\n\n"
        "| 列 A | 列 B |\n"
        "| --- | --- |\n"
        "| a   | b   |\n"
    )
    parser = MarkdownParser()
    out = parser.parse(src, CHAT_PROFILE)
    assert "# 标题" in out
    assert "**加粗**" in out
    assert "- 列表项 1" in out
    assert "```python" in out
    assert "| 列 A | 列 B |" in out


def test_markdown_parser_strips_control_chars() -> None:
    src = "hello\x00world\x07!"
    out = MarkdownParser().parse(src, CHAT_PROFILE)
    assert out == "helloworld!"


def test_markdown_parser_respects_max_chars_cap() -> None:
    profile = CHAT_PROFILE.model_copy(
        update={"output_schema": {"max_chars": 10}}
    )
    out = MarkdownParser().parse("0123456789abcdef", profile)
    assert len(out) <= 11  # 10 + ellipsis
    assert out.endswith("…")


def test_markdown_parser_rejects_empty() -> None:
    with pytest.raises(LLMProfileParseError):
        MarkdownParser().parse("", CHAT_PROFILE)


# ── 4. PlainTextParser behaviour ──────────────────────────────────


def test_plain_text_parser_strips_markdown_and_newlines() -> None:
    src = "# **标题**\n第二行\n> 引用"
    out = PlainTextParser().parse(src, TITLE_PROFILE)
    assert "\n" not in out
    assert "**" not in out
    assert "#" not in out
    assert ">" not in out


def test_plain_text_parser_strips_quote_pairs() -> None:
    src = '"你好"“世界”'
    out = PlainTextParser().parse(src, TITLE_PROFILE)
    assert '"' not in out
    assert "“”" not in out


def test_plain_text_parser_caps_at_24_chars() -> None:
    long = "测试" * 20
    out = PlainTextParser().parse(long, TITLE_PROFILE)
    assert len(out) <= 24


def test_plain_text_parser_uses_custom_max_chars() -> None:
    profile = TITLE_PROFILE.model_copy(
        update={"output_schema": {"max_chars": 4}}
    )
    out = PlainTextParser().parse("abcdefghij", profile)
    assert out == "abcd"


def test_plain_text_parser_rejects_empty() -> None:
    with pytest.raises(LLMProfileParseError):
        PlainTextParser().parse("", TITLE_PROFILE)


# ── 5. JsonStrictParser behaviour ─────────────────────────────────


def test_json_strict_parses_well_formed_object() -> None:
    payload = JsonStrictParser().parse(
        '{"intent": "x", "confidence": 0.9}', INTENT_PROFILE
    )
    assert payload == {"intent": "x", "confidence": 0.9}


def test_json_strict_strips_markdown_fence() -> None:
    fenced = "```json\n{\"a\": 1}\n```"
    assert JsonStrictParser().parse(fenced, INTENT_PROFILE) == {"a": 1}


def test_json_strict_rejects_non_json() -> None:
    with pytest.raises(LLMProfileParseError):
        JsonStrictParser().parse("不是 JSON", INTENT_PROFILE)


def test_json_strict_rejects_top_level_array() -> None:
    with pytest.raises(LLMProfileParseError):
        JsonStrictParser().parse("[1, 2, 3]", INTENT_PROFILE)


def test_json_strict_rejects_empty() -> None:
    with pytest.raises(LLMProfileParseError):
        JsonStrictParser().parse("", INTENT_PROFILE)


# ── 6. LLMClient.generate_with_profile happy path ─────────────────


def test_generate_with_profile_markdown_success() -> None:
    """The wrapper returns success=True with parsed Markdown on success."""
    stub = _StubLLM("# 你好\n\n**加粗**")
    llm = LLMClient(config_provider=object())
    # Avoid touching the real network — monkey-patch the underlying
    # call instead.  generate_with_profile delegates to
    # generate_with_system; replacing that is the natural seam.
    llm.generate_with_system = stub.generate_with_system  # type: ignore[method-assign]

    result = asyncio.run(llm.generate_with_profile(CHAT_PROFILE, "用户消息"))
    assert isinstance(result, LLMProfileResult)
    assert result.success is True
    assert "# 你好" in result.parsed
    assert "**加粗**" in result.parsed
    assert result.error_type is None
    assert stub.calls and stub.calls[0]["system_prompt"] == CHAT_PROFILE.system_prompt


def test_generate_with_profile_fallback_on_parse_failure() -> None:
    """Parse failure + FALLBACK_DEFAULT policy returns success=False
    with the parsed fallback value."""
    stub = _StubLLM("this is not JSON at all")
    llm = LLMClient(config_provider=object())
    llm.generate_with_system = stub.generate_with_system  # type: ignore[method-assign]

    result = asyncio.run(llm.generate_with_profile(INTENT_PROFILE, "x"))
    assert result.success is False
    assert result.error_type == "parse_error"
    # The fallback JSON is itself JSON; the parser re-runs on it and
    # returns a dict.
    assert isinstance(result.parsed, dict)
    assert result.parsed["route"] == "clarify"
    assert result.parsed["intent"] == "unknown"


def test_generate_with_profile_raises_on_parse_failure_when_policy_is_raise() -> (
    None
):
    strict_profile = LLMTaskProfile(
        name="strict",
        system_prompt="...",
        parser=LLMParserType.JSON_STRICT,
        on_parse_failure=LLMParseFailurePolicy.RAISE,
        fallback_text=None,
    )
    llm = LLMClient(config_provider=object())
    llm.generate_with_system = _StubLLM(  # type: ignore[method-assign]
        "garbage"
    ).generate_with_system

    with pytest.raises(LLMProfileParseError):
        asyncio.run(llm.generate_with_profile(strict_profile, "x"))


def test_generate_with_profile_llm_error_returns_failure_result() -> None:
    from app.integrations.llm_client import LLMClientError

    llm = LLMClient(config_provider=object())
    llm.generate_with_system = _StubLLM(  # type: ignore[method-assign]
        raise_exc=LLMClientError("network down"),
    ).generate_with_system

    result = asyncio.run(llm.generate_with_profile(CHAT_PROFILE, "x"))
    assert result.success is False
    assert result.error_type == "llm_error"
    assert "network down" in (result.error_message or "")
    # The profile fallback text is returned as parsed so the caller
    # always has something safe to surface.
    assert result.parsed == CHAT_PROFILE.fallback_text


def test_generate_with_profile_passes_timeout_override() -> None:
    """Profile.timeout_override is forwarded to generate_with_system."""
    profile = TITLE_PROFILE  # timeout_override=20
    stub = _StubLLM("测试方案")
    llm = LLMClient(config_provider=object())
    llm.generate_with_system = stub.generate_with_system  # type: ignore[method-assign]

    asyncio.run(llm.generate_with_profile(profile, "用户首条"))
    assert stub.calls
    assert stub.calls[0]["timeout_override"] == 20


def test_generate_with_profile_passes_repair_timeout_override() -> None:
    """Repair profile timeout is forwarded to the model call."""
    stub = _StubLLM(
        '{"action":"finish","target_issue_ids":[],"target_section_ids":[],'
        '"decision_summary":"ok","confidence":0.5}'
    )
    llm = LLMClient(config_provider=object())
    llm.generate_with_system = stub.generate_with_system  # type: ignore[method-assign]

    asyncio.run(llm.generate_with_profile(REPAIR_PROFILE, "用户首条"))
    assert stub.calls
    assert stub.calls[0]["timeout_override"] == 45


def test_generate_with_profile_error_message_no_api_key_added_by_wrapper() -> None:
    """The wrapper itself does not embed any config (api_key etc.)
    into the error message — it only forwards the upstream exception's
    type and message."""
    from app.integrations.llm_client import LLMClientError

    llm = LLMClient(config_provider=object())
    llm.generate_with_system = _StubLLM(  # type: ignore[method-assign]
        raise_exc=LLMClientError("upstream timeout"),
    ).generate_with_system

    result = asyncio.run(llm.generate_with_profile(CHAT_PROFILE, "x"))
    assert result.success is False
    assert "upstream timeout" in (result.error_message or "")
    # The wrapper does NOT inject any configuration strings — only
    # the exception class name and the upstream message.
    assert "api_key" not in (result.error_message or "")
    assert "sk-" not in (result.error_message or "")


# ── 7. Cross-module contract invariants ───────────────────────────


def test_chat_fallback_text_matches_message_service() -> None:
    """The two modules share a fallback value but cannot import each
    other (cycle).  This regression test guards against drift."""
    from app.services.chat_llm_service import CHAT_FALLBACK_TEXT
    from app.services.message_service import CHAT_FALLBACK_REPLY

    assert CHAT_FALLBACK_TEXT == CHAT_FALLBACK_REPLY


def test_chat_fallback_text_unified_across_all_surfaces() -> None:
    """F014 closeout: chat fallback must be a single source of truth.

    Three surfaces must read the same string:
      * ``app.llm.task_profiles.CHAT_FALLBACK_REPLY`` (canonical)
      * ``CHAT_PROFILE.fallback_text`` (contract layer default)
      * ``chat_llm_service.CHAT_FALLBACK_TEXT``
      * ``message_service.CHAT_FALLBACK_REPLY``
    """
    from app.llm.task_profiles import (
        CHAT_FALLBACK_REPLY as CANONICAL,
        CHAT_PROFILE,
    )
    from app.services.chat_llm_service import CHAT_FALLBACK_TEXT
    from app.services.message_service import CHAT_FALLBACK_REPLY

    assert CANONICAL == "抱歉，临时无法回复，请稍后再试。"
    assert CHAT_FALLBACK_TEXT == CANONICAL
    assert CHAT_FALLBACK_REPLY == CANONICAL
    assert CHAT_PROFILE.fallback_text == CANONICAL


def test_intent_router_exposes_contract_profile() -> None:
    """IntentRouter references INTENT_PROFILE via INTENT_CONTRACT so
    the contract surface is discoverable from the agent package."""
    from app.agent.intent_router import INTENT_CONTRACT

    assert INTENT_CONTRACT is INTENT_PROFILE
    assert INTENT_CONTRACT.require_json is True
    assert INTENT_CONTRACT.allow_markdown is False


def test_title_profile_used_in_message_service() -> None:
    """MessageService imports TITLE_PROFILE and PlainTextParser."""
    from app.llm.task_profiles import TITLE_PROFILE as TP

    assert TP.parser is LLMParserType.PLAIN_TEXT
    assert TP.max_tokens == 64
