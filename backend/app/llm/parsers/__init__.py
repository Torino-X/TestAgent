"""Parser adapters for the LLM task contract layer (F014).

Each adapter implements the ``ParserAdapter`` protocol:

    parse(raw_text: str, profile: LLMTaskProfile) -> Any

Adapters are stateless and side-effect-free.  They may raise
``LLMProfileParseError`` on failure; the wrapper
``LLMClient.generate_with_profile`` translates that into a successful
``LLMProfileResult(success=False, ...)`` when the profile's
``on_parse_failure`` is ``FALLBACK_DEFAULT``.
"""

from app.llm.parsers.json_relaxed import JsonRelaxedParser
from app.llm.parsers.json_strict import JsonStrictParser
from app.llm.parsers.markdown import MarkdownParser
from app.llm.parsers.plain_text import PlainTextParser
from app.llm.parsers.raw_text import RawTextParser
from app.llm.parsers.protocol import ParserAdapter

__all__ = [
    "ParserAdapter",
    "JsonStrictParser",
    "JsonRelaxedParser",
    "PlainTextParser",
    "MarkdownParser",
    "RawTextParser",
]# llm.parsers:ParserAdapter 实现集合(json_strict / json_relaxed / plain_text / markdown);统一由 generate_with_profile 调度。
