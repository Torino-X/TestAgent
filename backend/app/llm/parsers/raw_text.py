"""Raw-text parser used when a downstream component owns validation.

Unlike the plain-text and Markdown parsers, this adapter deliberately makes
no formatting, Unicode, or whitespace changes. It is intended for call sites
that must preserve the provider response verbatim and validate it later.
"""

from __future__ import annotations

from typing import Any

from app.llm.errors import LLMProfileParseError
from app.llm.task_profiles import LLMTaskProfile


class RawTextParser:
    """Return the provider response unchanged, rejecting only an empty value."""

    def parse(self, raw_text: str, profile: LLMTaskProfile) -> Any:  # noqa: ARG002
        if not raw_text:
            raise LLMProfileParseError("empty raw-text content")
        return raw_text
