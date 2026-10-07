"""Plain text parser — strips Markdown and formatting (F014).

Used by conversation-title generation (and any short-text contract
where Markdown is not desired).  Output is always a single line:

  * Markdown heading prefixes (``#``/``##``) removed
  * Inline code spans and code fences unwrapped (content only)
  * Bold / italic markers removed (``**x**`` → ``x``)
  * Quote / list / checkbox markers removed
  * HTML tags stripped
  * Newlines collapsed to single spaces
  * Leading / trailing quote pairs (CJK + ASCII) removed
  * Excess whitespace collapsed
  * Truncated to ``output_schema['max_chars']`` if set (default 24)
"""

from __future__ import annotations

import re
from typing import Any

from app.llm.errors import LLMProfileParseError
from app.llm.task_profiles import LLMTaskProfile


_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BOLD_ITALIC_RE = re.compile(r"(\*\*|__|\*|_)(.*?)\1")
_CODE_FENCE_RE = re.compile(r"```[^\n]*\n?(.*?)\n?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_LIST_MARKER_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d+\.)\s+", re.MULTILINE)
_CHECKBOX_RE = re.compile(r"^\s{0,3}\[[ xX]\]\s+", re.MULTILINE)
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
# Characters considered "quote / wrapper" punctuation.  Includes
# ASCII single/double quotes, backticks, CJK quotation marks, angle
# brackets, and common sentence punctuation.
_QUOTE_CHARS = (
    "\"'`"
    "“”"
    "‘’"
    "《》"
    "「」"
    "『』"
)
_QUOTE_ANYWHERE_RE = re.compile("[" + _QUOTE_CHARS + "]")
_TRAILING_PUNCT_RE = re.compile(r"[.,;:!?。，；：！？]+$")


def _strip_to_plain(text: str) -> str:
    if not text:
        return ""
    text = _CODE_FENCE_RE.sub(r"\1", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _HEADING_RE.sub("", text)
    text = _LIST_MARKER_RE.sub("", text)
    text = _CHECKBOX_RE.sub("", text)
    text = _QUOTE_RE.sub("", text)
    text = _BOLD_ITALIC_RE.sub(r"\2", text)
    text = _HTML_TAG_RE.sub("", text)
    # Strip ALL wrapper/quote characters (anywhere in the text — model
    # sometimes opens/closes a quote mid-string).
    text = _QUOTE_ANYWHERE_RE.sub("", text)
    # Newlines & tabs → single space
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Strip trailing sentence punctuation (commas / periods / semicolons
    # / colons / exclamation / question marks, ASCII and CJK).
    while True:
        new = _TRAILING_PUNCT_RE.sub("", text)
        if new == text:
            break
        text = new.strip()
    return text.strip()


class PlainTextParser:
    """Adapter that produces a single-line plain-text string.

    ``profile.output_schema`` may contain ``max_chars`` (default 24).
    """

    def parse(self, raw_text: str, profile: LLMTaskProfile) -> Any:
        text = _strip_to_plain(raw_text or "")
        if not text:
            raise LLMProfileParseError("empty plain-text content")
        cap = 24
        if isinstance(profile.output_schema, dict):
            raw_cap = profile.output_schema.get("max_chars")
            if isinstance(raw_cap, int) and raw_cap > 0:
                cap = raw_cap
        if len(text) > cap:
            text = text[:cap].rstrip()
        return text# llm.parsers.plain_text:剥离 Markdown 的纯文本解析器(用于会话标题等短文本契约);规范化换行/引号/空白 + max_chars 截断。
