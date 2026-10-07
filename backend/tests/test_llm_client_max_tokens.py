"""Tests for LLMClient — 2026-07 ``max_tokens`` wire-payload coverage.

These tests exist because the ``TEST_PLAN_PROFILE.max_tokens`` field is
dead config: ``TestPlanGeneratorTool`` calls ``LLMClient.generate()``,
which never reads the profile.  Until this commit, the resolved value
was simply dropped — the upstream ``chat.completions.create`` call
didn't include ``max_tokens`` at all, and provider defaults (often 4096)
silently truncated 23k-character Chinese test-plan responses.

Tests here lock in:
1. ``_resolve_max_tokens`` returns the right value for each priority
   level (cfg > protocol fallback > None).
2. ``chat.completions.create`` is invoked with ``max_tokens`` set
   (for both streaming and non-streaming paths).
3. TestPlanGeneratorTool gets a non-None ``max_tokens`` on the wire
   when the user has not configured one (was None before this commit).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest


@dataclass
class _FakeCfg:
    """Minimal stub for the LLMClient config provider."""

    api_url: str = "https://api.example.com/v1"
    api_key: str = "sk-test"
    model_name: str = "qwen-plus"
    timeout: int = 90
    enable_thinking: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None


# Import target after the dataclass is defined so the module-level
# resolver function is exercised by the same import path the real
# code uses.
from app.integrations.llm_client import (
    LLMClient,
    _DEFAULT_TEST_PLAN_MAX_TOKENS,
    _DEFAULT_TEST_PLAN_TEMPERATURE,
    _TEST_PLAN_SYSTEM_PROMPT,
    _resolve_max_tokens,
    _resolve_temperature,
)


# ═══════════════════════════════════════════════════════════════════
# _resolve_max_tokens — pure function, no async needed
# ═══════════════════════════════════════════════════════════════════


class TestResolveMaxTokens:
    def test_user_configured_value_wins(self):
        cfg = _FakeCfg(max_tokens=4096)
        assert _resolve_max_tokens(cfg, _TEST_PLAN_SYSTEM_PROMPT) == 4096
        assert _resolve_max_tokens(cfg, "随便聊聊天") == 4096

    def test_protocol_fallback_for_test_plan_prompt(self):
        cfg = _FakeCfg(max_tokens=None)
        assert _resolve_max_tokens(cfg, _TEST_PLAN_SYSTEM_PROMPT) == (
            _DEFAULT_TEST_PLAN_MAX_TOKENS
        )
        assert _DEFAULT_TEST_PLAN_MAX_TOKENS >= 16000, (
            "23k+ char Chinese responses need ≥16k tokens; "
            "the 8000 default was the root cause of the truncation bug."
        )

    def test_none_for_other_call_sites_when_unset(self):
        """CHAT/INTENT/TITLE/SUMMARY/Vision etc. defer to provider default
        when the user hasn't set cfg.max_tokens — bumping them would just
        raise cost with no benefit."""
        cfg = _FakeCfg(max_tokens=None)
        assert _resolve_max_tokens(cfg, "你是 TestAgent...") is None
        assert _resolve_max_tokens(cfg, "你是意图路由器...") is None
        assert _resolve_max_tokens(cfg, "只输出标题...") is None

    def test_zero_is_treated_as_unset(self):
        """``0`` in DB means 'not configured'; don't pass it as a token
        cap to the upstream API."""
        cfg = _FakeCfg(max_tokens=0)
        # For non-test-plan prompts, 0 should fall through to None:
        assert _resolve_max_tokens(cfg, "你是 TestAgent...") is None
        # For test_plan prompts, 0 should still hit the protocol fallback:
        assert _resolve_max_tokens(cfg, _TEST_PLAN_SYSTEM_PROMPT) == (
            _DEFAULT_TEST_PLAN_MAX_TOKENS
        )

    def test_stripped_system_prompt_still_matches(self):
        """PromptBuilder may prepend whitespace; the resolver strips
        before matching the system prompt anchor."""
        cfg = _FakeCfg(max_tokens=None)
        padded = "   \n  " + _TEST_PLAN_SYSTEM_PROMPT
        assert _resolve_max_tokens(cfg, padded) == _DEFAULT_TEST_PLAN_MAX_TOKENS


# ═══════════════════════════════════════════════════════════════════
# Wire-payload tests — assert ``max_tokens`` actually reaches the API
# ═══════════════════════════════════════════════════════════════════


class TestMaxTokensOnWire:
    """Drive ``LLMClient.generate`` and ``stream_with_system`` with a
    mocked openai client; capture the kwargs that reach the upstream
    ``chat.completions.create`` call."""

    @pytest.fixture
    def captured(self) -> Dict[str, Any]:
        """Records each call's kwargs so tests can assert on them."""
        return {}

    @pytest.fixture
    def patch_openai(self, captured):
        """Patch AsyncOpenAI so ``chat.completions.create`` is an AsyncMock
        that records its kwargs into ``captured``."""

        def _make_non_stream_completion() -> Any:
            completion = MagicMock()
            completion.choices = [MagicMock()]
            completion.choices[0].message.content = "ok"
            return completion

        def _factory(*args: Any, **kwargs: Any) -> Any:
            client = MagicMock()

            async def _create(**kw: Any) -> Any:
                captured.setdefault("calls", []).append(kw)
                if kw.get("stream"):
                    # Yield a single chunk with content so the consumer
                    # loop emits at least once (otherwise it raises
                    # "模型响应中未找到可用内容").
                    chunk = MagicMock()
                    chunk.choices = [MagicMock()]
                    chunk.choices[0].delta.content = "ok"
                    return _async_iter([chunk])
                return _make_non_stream_completion()

            async def _async_iter(items):
                for item in items:
                    yield item

            client.chat.completions.create = _create
            return client

        with patch("openai.AsyncOpenAI", side_effect=_factory) as mock_cls:
            yield mock_cls, captured

    @pytest.mark.asyncio
    async def test_generate_test_plan_carries_max_tokens(self, patch_openai):
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg(max_tokens=None))

        answer = await client.generate("dummy prompt")

        assert answer == "ok"
        assert len(captured["calls"]) == 1
        kwargs = captured["calls"][0]
        assert kwargs.get("max_tokens") == _DEFAULT_TEST_PLAN_MAX_TOKENS, (
            "max_tokens must be on the wire for test_plan generation; "
            "previously it was dropped entirely."
        )
        assert kwargs["stream"] is False

    @pytest.mark.asyncio
    async def test_generate_user_override_wins(self, patch_openai):
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg(max_tokens=8192))

        await client.generate("dummy prompt")

        kwargs = captured["calls"][0]
        assert kwargs["max_tokens"] == 8192

    @pytest.mark.asyncio
    async def test_generate_with_system_test_plan_carries_max_tokens(
        self, patch_openai
    ):
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg(max_tokens=None))

        # Use the exact _TEST_PLAN_SYSTEM_PROMPT so the heuristic kicks in
        await client.generate_with_system(_TEST_PLAN_SYSTEM_PROMPT, "user content")

        kwargs = captured["calls"][0]
        assert kwargs["max_tokens"] == _DEFAULT_TEST_PLAN_MAX_TOKENS

    @pytest.mark.asyncio
    async def test_generate_with_system_chat_omits_max_tokens(self, patch_openai):
        """Non-test-plan call sites pass no ``max_tokens`` — let the
        provider choose.  Anything else would inflate cost for short
        replies (chat, intent, title)."""
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg(max_tokens=None))

        await client.generate_with_system("你是 TestAgent...", "hi")

        kwargs = captured["calls"][0]
        assert "max_tokens" not in kwargs, (
            f"non-test-plan call leaked max_tokens={kwargs.get('max_tokens')}"
        )

    @pytest.mark.asyncio
    async def test_stream_path_also_carries_max_tokens(self, patch_openai):
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg(max_tokens=None))

        chunks: List[str] = []
        async for piece in client.stream_with_system(
            _TEST_PLAN_SYSTEM_PROMPT, "user content"
        ):
            chunks.append(piece)

        assert len(captured["calls"]) == 1
        kwargs = captured["calls"][0]
        assert kwargs["stream"] is True
        assert kwargs["stream_options"] == {"include_usage": True}
        assert kwargs["max_tokens"] == _DEFAULT_TEST_PLAN_MAX_TOKENS, (
            "streaming test_plan generation also needs max_tokens "
            "on the wire — same 23k-char truncation bug otherwise."
        )


# ═══════════════════════════════════════════════════════════════════
# Profile metadata — sanity-check the documented contract
# ═══════════════════════════════════════════════════════════════════


class TestTestPlanProfileContract:
    def test_profile_max_tokens_is_documented(self):
        from app.llm.task_profiles import TEST_PLAN_PROFILE
        assert TEST_PLAN_PROFILE.max_tokens == 16000, (
            "If you change TEST_PLAN_PROFILE.max_tokens, also update "
            "13_TestAgent_测试方案生成提示词结构.md §4.3 to match."
        )

    def test_profile_timeout_matches_wire_resolver(self):
        """TEST_PLAN_PROFILE.timeout_override (90s) is the Profile-level
        default.  ``_resolve_effective_timeout`` reads it as the
        Profile default (only when the user has not configured an
        explicit value), and the live effective value comes from
        ``_DEFAULT_TEST_PLAN_TIMEOUT`` in ``llm_client.py``.  The two
        must match or the Profile default is silently bypassed."""
        from app.llm.task_profiles import TEST_PLAN_PROFILE
        from app.integrations.llm_client import _DEFAULT_TEST_PLAN_TIMEOUT
        assert TEST_PLAN_PROFILE.timeout_override == 90
        assert _DEFAULT_TEST_PLAN_TIMEOUT == 90, (
            "_DEFAULT_TEST_PLAN_TIMEOUT and TEST_PLAN_PROFILE.timeout_override "
            "must stay in lockstep — the resolver reads the constant, the "
            "profile is the documented contract."
        )


# ═══════════════════════════════════════════════════════════════════
# _resolve_temperature — mirror of the max_tokens coverage
# ═══════════════════════════════════════════════════════════════════
#
# The same dead-config problem exists for ``temperature``: ``TEST_PLAN_PROFILE.
# temperature=0.2`` was never read by ``LLMClient.generate()``.  Without an
# explicit resolver, DashScope / OpenAI defaults to 1.0, which causes JSON
# structure to drift between calls and intermittently breaks schema
# validation.  These tests lock in the resolver's 3-tier priority.


class TestResolveTemperature:
    def test_user_configured_value_wins(self):
        cfg = _FakeCfg(temperature=0.5)
        assert _resolve_temperature(cfg, _TEST_PLAN_SYSTEM_PROMPT) == 0.5
        assert _resolve_temperature(cfg, "随便聊聊天") == 0.5

    def test_protocol_fallback_for_test_plan_prompt(self):
        cfg = _FakeCfg(temperature=None)
        assert _resolve_temperature(cfg, _TEST_PLAN_SYSTEM_PROMPT) == (
            _DEFAULT_TEST_PLAN_TEMPERATURE
        )
        # 1.0 is the typical provider default — anything that high makes
        # the JSON output non-deterministic.  0.2 is the documented sweet
        # spot for structured-output tasks.
        assert _DEFAULT_TEST_PLAN_TEMPERATURE <= 0.3

    def test_none_for_other_call_sites_when_unset(self):
        """CHAT (0.7) / INTENT (0.0) / TITLE (0.3) / SUMMARY (0.2) each
        have their own tuned value and the user can already override per-
        config.  Don't leak the test_plan default into them."""
        cfg = _FakeCfg(temperature=None)
        assert _resolve_temperature(cfg, "你是 TestAgent...") is None
        assert _resolve_temperature(cfg, "你是意图路由器...") is None
        assert _resolve_temperature(cfg, "只输出标题...") is None

    def test_zero_is_treated_as_a_real_value(self):
        """Unlike ``max_tokens=0`` (which DB-stored as 0 means 'unset'),
        ``temperature=0`` is a real value (deterministic).  ``getattr`` with
        default ``None`` distinguishes the two: ``0.0`` is falsy but not
        None.  This test guards against accidentally changing the
        resolver to use ``if cfg_temp:`` (which would skip 0.0)."""
        cfg = _FakeCfg(temperature=0.0)
        assert _resolve_temperature(cfg, _TEST_PLAN_SYSTEM_PROMPT) == 0.0
        assert _resolve_temperature(cfg, "你是 TestAgent...") == 0.0

    def test_stripped_system_prompt_still_matches(self):
        cfg = _FakeCfg(temperature=None)
        padded = "   \n  " + _TEST_PLAN_SYSTEM_PROMPT
        assert _resolve_temperature(cfg, padded) == _DEFAULT_TEST_PLAN_TEMPERATURE


class TestTemperatureOnWire:
    """Drive ``LLMClient.generate`` / ``stream_with_system`` with a mocked
    openai client; assert ``temperature`` actually reaches the upstream
    ``chat.completions.create`` call (was missing entirely before)."""

    @pytest.fixture
    def captured(self) -> Dict[str, Any]:
        return {}

    @pytest.fixture
    def patch_openai(self, captured):
        def _make_non_stream_completion() -> Any:
            completion = MagicMock()
            completion.choices = [MagicMock()]
            completion.choices[0].message.content = "ok"
            return completion

        def _factory(*args: Any, **kwargs: Any) -> Any:
            client = MagicMock()

            async def _create(**kw: Any) -> Any:
                captured.setdefault("calls", []).append(kw)
                if kw.get("stream"):
                    chunk = MagicMock()
                    chunk.choices = [MagicMock()]
                    chunk.choices[0].delta.content = "ok"

                    async def _async_iter(items):
                        for item in items:
                            yield item

                    return _async_iter([chunk])
                return _make_non_stream_completion()

            client.chat.completions.create = _create
            return client

        with patch("openai.AsyncOpenAI", side_effect=_factory) as mock_cls:
            yield mock_cls, captured

    @pytest.mark.asyncio
    async def test_generate_test_plan_carries_temperature(self, patch_openai):
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg(temperature=None))

        await client.generate("dummy prompt")

        assert len(captured["calls"]) == 1
        kwargs = captured["calls"][0]
        assert kwargs.get("temperature") == _DEFAULT_TEST_PLAN_TEMPERATURE, (
            "temperature must be on the wire for test_plan generation; "
            "previously it was dropped entirely and provider default 1.0 "
            "destabilised JSON output."
        )

    @pytest.mark.asyncio
    async def test_generate_user_override_wins(self, patch_openai):
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg(temperature=0.5))

        await client.generate("dummy prompt")

        kwargs = captured["calls"][0]
        assert kwargs["temperature"] == 0.5

    @pytest.mark.asyncio
    async def test_generate_with_system_chat_omits_temperature(self, patch_openai):
        """Non-test-plan call sites pass no ``temperature`` — let the
        provider choose (or the upstream ``generate_with_profile`` path
        supply its own value from the profile)."""
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg(temperature=None))

        await client.generate_with_system("你是 TestAgent...", "hi")

        kwargs = captured["calls"][0]
        assert "temperature" not in kwargs, (
            f"non-test_plan call leaked temperature={kwargs.get('temperature')}"
        )

    @pytest.mark.asyncio
    async def test_stream_path_also_carries_temperature(self, patch_openai):
        mock_cls, captured = patch_openai
        client = LLMClient(config_provider=_FakeCfg(temperature=None))

        chunks: List[str] = []
        async for piece in client.stream_with_system(
            _TEST_PLAN_SYSTEM_PROMPT, "user content"
        ):
            chunks.append(piece)

        assert len(captured["calls"]) == 1
        kwargs = captured["calls"][0]
        assert kwargs["stream"] is True
        assert kwargs["temperature"] == _DEFAULT_TEST_PLAN_TEMPERATURE


class TestTestPlanProfileContractTemperature:
    def test_profile_temperature_is_documented(self):
        from app.llm.task_profiles import TEST_PLAN_PROFILE
        assert TEST_PLAN_PROFILE.temperature == _DEFAULT_TEST_PLAN_TEMPERATURE, (
            "If you change TEST_PLAN_PROFILE.temperature, also update "
            "_DEFAULT_TEST_PLAN_TEMPERATURE in llm_client.py — they must "
            "stay in lockstep (the resolver fallback mirrors the profile)."
        )

    def test_default_temperature_matches_resolver_fallback(self):
        """The wire-level fallback must equal the documented profile
        value.  Drift between them means either (a) the profile change
        wasn't mirrored to the resolver, or (b) the resolver change
        wasn't mirrored to the profile — either way, silent regression."""
        from app.llm.task_profiles import TEST_PLAN_PROFILE
        assert TEST_PLAN_PROFILE.temperature == _DEFAULT_TEST_PLAN_TEMPERATURE
