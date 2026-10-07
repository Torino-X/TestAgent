"""Relaxed JSON parser (F014).

Reserved for future contracts that need a more forgiving JSON
extraction than ``JsonStrictParser`` — e.g. when the model tends to
emit partial / nested objects.

For this batch the relaxed parser delegates to ``JsonStrictParser``
and only differs in **error reporting** (the prefix on the raised
error mentions ``json_relaxed``).  This keeps the public surface
stable while letting future work fork the strategy without changing
callers.

Adding real relaxed behaviour later (e.g. accepting arrays at top
level, tolerating trailing commas, repairing missing brackets) is a
localised change in this file only.
"""

from __future__ import annotations

from typing import Any

from app.common.result_parser import ResultParser
from app.llm.errors import LLMProfileParseError
from app.llm.parsers.json_strict import JsonStrictParser
from app.llm.task_profiles import LLMTaskProfile


class JsonRelaxedParser:
    """Forgiving JSON parser.

    Currently delegates to ``JsonStrictParser``; the error prefix is
    rewritten so callers and logs can distinguish the two paths.
    """

    def __init__(self, result_parser: ResultParser | None = None) -> None:
        self._inner = JsonStrictParser(result_parser=result_parser)

    def parse(self, raw_text: str, profile: LLMTaskProfile) -> Any:
        try:
            return self._inner.parse(raw_text, profile)
        except LLMProfileParseError as exc:
            raise LLMProfileParseError(
                f"relaxed-JSON parse failed: {exc}".replace(
                    "strict-JSON", "relaxed-JSON"
                )
            ) from exc# llm.parsers.json_relaxed:宽松 JSON 解析器(目前委托 json_strict + 错误前缀区分);预留未来容忍部分 JSON / 尾逗号 / 缺括号等扩展点。
