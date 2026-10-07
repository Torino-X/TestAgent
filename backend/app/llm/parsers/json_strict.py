"""Strict JSON parser — reuses ``ResultParser.parse_json`` (F014).

Used by intent recognition (and any contract that promises a JSON
object).  Delegates the actual extraction to the existing
``ResultParser`` so we do not duplicate fence-stripping / bracket
depth-scan logic.

Behaviour:

  * Markdown `` ```json ... ``` `` fences are accepted and stripped.
  * Prose before/after the JSON object is tolerated (brace-scan).
  * Top-level JSON must be an object — arrays / scalars are rejected.
  * Whitespace around JSON key names is normalised (``" k"`` → ``"k"``).
  * Failure raises ``LLMProfileParseError`` (NOT
    ``ResultParseError`` directly) so callers can catch one type.

If the profile carries an ``output_schema`` dict, we *reserve* the
validation hook (logged, not enforced) — schema validation belongs to
the caller's Pydantic model, not the parser.
"""

from __future__ import annotations

import logging
from typing import Any

from app.common.result_parser import ResultParseError, ResultParser
from app.llm.errors import LLMProfileParseError
from app.llm.task_profiles import LLMTaskProfile

logger = logging.getLogger(__name__)


class JsonStrictParser:
    """Parser that yields a Python dict from a strict-JSON contract."""

    def __init__(self, result_parser: ResultParser | None = None) -> None:
        self._parser = result_parser or ResultParser()

    def parse(self, raw_text: str, profile: LLMTaskProfile) -> Any:
        if not raw_text or not raw_text.strip():
            logger.warning(
                "JsonStrictParser: 拒绝空响应 | profile=%s | 原因=empty",
                profile.name,
            )
            raise LLMProfileParseError("empty model response")
        try:
            payload = self._parser.parse_json(raw_text)
        except ResultParseError as first_exc:
            # Strict 失败 → 兜底走 lenient 解析(截断/残留 fence/嵌套 prose 都能扛)。
            # lenient 已有完整实现(parse_json_lenient + _slice_and_parse),
            # 这里只接上自动 fallback,不重写宽容逻辑。
            try:
                partial, _issues = self._parser.parse_json_lenient(raw_text, config=None)
            except ResultParseError:
                logger.warning(
                    "JsonStrictParser: 拒绝响应 | profile=%s | 原因=strict+lenient 都失败 | 长度=%d",
                    profile.name, len(raw_text),
                )
                raise LLMProfileParseError(
                    f"strict-JSON parse failed: {first_exc}"
                ) from first_exc
            if not isinstance(partial, dict):
                logger.warning(
                    "JsonStrictParser: 拒绝响应 | profile=%s | 原因=lenient top-level 非 object | 长度=%d",
                    profile.name, len(raw_text),
                )
                raise LLMProfileParseError(
                    f"strict-JSON parse failed: {first_exc}"
                ) from first_exc
            logger.info(
                "JsonStrictParser: lenient 兜底成功 | profile=%s | keys=%s | 长度=%d",
                profile.name, sorted(partial.keys()), len(raw_text),
            )
            payload = partial
        if not isinstance(payload, dict):
            logger.warning(
                "JsonStrictParser: 拒绝响应 | profile=%s | 原因=top-level 非 object (got %s) | 长度=%d",
                profile.name, type(payload).__name__, len(raw_text),
            )
            raise LLMProfileParseError(
                f"strict-JSON top-level must be an object, got {type(payload).__name__}"
            )
        # Schema hook — reserved for future structured-output use.
        if profile.output_schema:
            logger.debug(
                "JsonStrictParser: output_schema provided for profile %s "
                "(validation deferred to caller Pydantic model)",
                profile.name,
            )
        return payload