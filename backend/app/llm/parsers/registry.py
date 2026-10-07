"""Parser registry (F014).

Maps ``LLMParserType`` enum values to concrete ``ParserAdapter``
instances.  Parser instances are stateless and may be reused; the
registry exposes a factory ``get_parser`` that constructs them lazily
on first use and caches them.

Adding a new parser type:

  1. Add the enum member in ``app.llm.task_profiles``.
  2. Implement a class in this package satisfying ``ParserAdapter``.
  3. Register it in ``_PARSER_FACTORIES`` below.

The registry raises ``LLMProfileConfigError`` (not ``KeyError``) for
unknown parser types so the calling surface stays consistent.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.llm.errors import LLMProfileConfigError
from app.llm.parsers.json_relaxed import JsonRelaxedParser
from app.llm.parsers.json_strict import JsonStrictParser
from app.llm.parsers.markdown import MarkdownParser
from app.llm.parsers.plain_text import PlainTextParser
from app.llm.parsers.raw_text import RawTextParser
from app.llm.parsers.protocol import ParserAdapter
from app.llm.task_profiles import LLMParserType

logger = logging.getLogger(__name__)


_PARSER_FACTORIES: dict[LLMParserType, Callable[[], ParserAdapter]] = {
    LLMParserType.JSON_STRICT: JsonStrictParser,
    LLMParserType.JSON_RELAXED: JsonRelaxedParser,
    LLMParserType.PLAIN_TEXT: PlainTextParser,
    LLMParserType.MARKDOWN: MarkdownParser,
    LLMParserType.RAW_TEXT: RawTextParser,
}

_cached_parsers: dict[LLMParserType, ParserAdapter] = {}


def get_parser(
    parser_type: LLMParserType,
    *,
    cached: bool = True,
) -> ParserAdapter:
    """Return a parser instance for ``parser_type``.

    Set ``cached=False`` to construct a fresh instance (used by tests
    that need to inject a stubbed ``ResultParser``).
    """
    factory = _PARSER_FACTORIES.get(parser_type)
    if factory is None:
        raise LLMProfileConfigError(
            f"no parser registered for {parser_type!r}; "
            f"available: {sorted(p.value for p in _PARSER_FACTORIES)}"
        )
    if cached and parser_type in _cached_parsers:
        logger.debug("ParserRegistry.get_parser: 缓存命中 | type=%s", parser_type.value)
        return _cached_parsers[parser_type]
    instance = factory()
    if cached:
        _cached_parsers[parser_type] = instance
        logger.debug("ParserRegistry.get_parser: 缓存未命中, 已实例化 | type=%s", parser_type.value)
    return instance


def reset_parser_cache() -> None:
    """Clear the parser cache — intended for tests only."""
    _cached_parsers.clear()
