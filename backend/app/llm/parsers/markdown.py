"""Markdown parser — preserves Markdown structure (F014).

Used by ordinary chat replies (and future knowledge-base Q&A).  Unlike
the strict JSON path, Markdown content is the *expected* output, not
an error condition.  We only strip:

  * ASCII control characters (except common whitespace),
  * zero-width / BOM / stray directional markers,
  * trailing whitespace per line.

We deliberately do NOT:

  * remove Markdown headings, lists, code fences, tables, or bold,
  * truncate normal content (caller passes ``max_chars`` via the
    profile if needed),
  * call ``ResultParser.parse_json`` — Markdown is not an error.

The dangerous control-label regex that lives in
``ResultParser._sanitize_text`` (``【AI生成】`` etc.) is **not**
applied here.  Those labels only make sense in test-plan section
content; in chat replies the model legitimately uses the same
characters in normal prose (e.g. "AI 生成的内容质量评估…").
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.llm.errors import LLMProfileParseError
from app.llm.task_profiles import LLMTaskProfile


# Control chars except \n \r \t (allowed whitespace).
_CONTROL_CHARS_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)
# Zero-width, BOM, and bidirectional markers (privacy / rendering
# hazards).  All codepoints are listed explicitly so the source file
# contains no raw invisible characters — keeps the file portable
# through editors / tooling that may mangle such bytes.
_INVISIBLE_CODEPOINTS = (
    0x200B,  # ZERO WIDTH SPACE
    0x200C,  # ZERO WIDTH NON-JOINER
    0x200D,  # ZERO WIDTH JOINER
    0x200E,  # LEFT-TO-RIGHT MARK
    0x200F,  # RIGHT-TO-LEFT MARK
    0x202A,  # LEFT-TO-RIGHT EMBEDDING
    0x202B,  # RIGHT-TO-LEFT EMBEDDING
    0x202C,  # POP DIRECTIONAL FORMATTING
    0x202D,  # LEFT-TO-RIGHT OVERRIDE
    0x202E,  # RIGHT-TO-LEFT OVERRIDE
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
    0x202F,  # NARROW NO-BREAK SPACE
    0x3000,  # IDEOGRAPHIC SPACE (sometimes injected)
)
_INVISIBLE_RE = re.compile(
    "[" + "".join(chr(cp) for cp in _INVISIBLE_CODEPOINTS) + "]"
)


def _sanitize_safe_markdown(text: str) -> str:
    """Lightly clean Markdown text without altering its structure."""
    if not text:
        return ""
    # Normalize unicode (NFC) — fixes composed/decomposed form drift.
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL_CHARS_RE.sub("", text)
    text = _INVISIBLE_RE.sub("", text)
    # Per-line trailing whitespace; collapse runs of blank lines.
    lines = [line.rstrip() for line in text.split("\n")]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


class MarkdownParser:
    """Adapter that preserves Markdown structure.

    Truncates to ``profile.output_schema`` field ``max_chars`` if
    provided (used for length-capping); otherwise returns the cleaned
    text untouched.
    """

    def parse(self, raw_text: str, profile: LLMTaskProfile) -> Any:
        text = _sanitize_safe_markdown(raw_text or "")
        if not text:
            raise LLMProfileParseError("empty Markdown content")
        # Optional length cap via output_schema={"max_chars": N}
        if isinstance(profile.output_schema, dict):
            cap = profile.output_schema.get("max_chars")
            if isinstance(cap, int) and cap > 0 and len(text) > cap:
                text = text[:cap].rstrip() + "…"
        return text