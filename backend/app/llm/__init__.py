"""LLM task contract layer (F014).

Provides a thin, declarative contract layer on top of the existing
``LLMClient`` so that each call site explicitly states:

  * which system persona to use,
  * which output parser to apply (strict JSON, relaxed JSON, plain
    text, or Markdown text),
  * what to do when parsing fails (raise, fallback to a default value,
    or retry — currently only the first two are wired up).

The goal is **explicit output contracts**, not a new gateway.
``LLMClient`` continues to be the single underlying model client; this
package only adds the contract + parser pieces on top.

Usage::

    from app.llm.task_profiles import CHAT_PROFILE
    from app.integrations.llm_client import LLMClient

    client = LLMClient(config_provider=...)
    result = await client.generate_with_profile(CHAT_PROFILE, "用户消息")
    if result.success:
        text = result.parsed   # already parsed Markdown / plain text
    else:
        text = CHAT_PROFILE.fallback_text
"""

from app.llm.errors import LLMProfileError, LLMProfileParseError
from app.llm.task_profiles import (
    CHAT_PROFILE,
    INTENT_PROFILE,
    TEST_PLAN_PROFILE,
    TITLE_PROFILE,
    LLMParserType,
    LLMParseFailurePolicy,
    LLMTaskProfile,
)

__all__ = [
    "LLMParserType",
    "LLMParseFailurePolicy",
    "LLMTaskProfile",
    "CHAT_PROFILE",
    "INTENT_PROFILE",
    "TITLE_PROFILE",
    "TEST_PLAN_PROFILE",
    "LLMProfileError",
    "LLMProfileParseError",
]# llm 子包:F014 LLM 任务契约层(LLMTaskProfile + ParserAdapter + wrapper);不替代 LLMClient,只在其上挂显式 output contract。
