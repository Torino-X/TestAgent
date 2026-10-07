"""Parser adapter protocol (F014).

A parser adapter receives the raw LLM text and the profile that
describes how to interpret it, and returns a parsed value (``str`` or
``dict``, depending on the parser) or raises ``LLMProfileParseError``.

Adapters MUST be stateless: they may be reused across requests and
must not cache per-profile data.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.llm.task_profiles import LLMTaskProfile


@runtime_checkable
class ParserAdapter(Protocol):
    """Convert raw LLM text into a typed result per the profile contract."""

    def parse(self, raw_text: str, profile: LLMTaskProfile) -> Any:
        ...# llm.parsers.protocol:ParserAdapter 协议(parse(raw_text, profile) -> Any);实现必须无状态、可跨请求复用。
