"""JSON utilities — extraction from LLM responses and payload normalization.

``extract_json_from_llm_response`` handles Markdown-fenced and bracket-matched
LLM output.  ``normalize_json_object`` coerces MySQL JSON column values
(which aiomysql may return as strings) to plain dicts.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict

logger = logging.getLogger(__name__)


def extract_json_from_llm_response(text: str) -> Dict[str, Any]:
    """Extract a JSON object from an LLM response.

    Supports:
    - Pure JSON string
    - Markdown-fenced JSON (``````json ... `````)
    - Mixed text where the first complete JSON object is bracket-extracted

    Args:
        text: Raw LLM response text.

    Returns:
        Parsed dict.

    Raises:
        ValueError: No valid JSON object found.
    """
    text = text.strip()

    # Strip a single surrounding Markdown fence
    fence = re.match(r"^```(?:json|JSON)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Bracket-depth scan for the first complete JSON object
    start = text.find("{")
    if start < 0:
        raise ValueError("响应中未找到 JSON 对象")

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : index + 1])

    raise ValueError("JSON 对象不完整")


def normalize_json_object(value: Any) -> dict:
    """Coerce a MySQL JSON column value to a dict.

    aiomysql may return JSON columns as Python strings instead of dicts.
    This function ensures consistent dict output for API responses.

    Rules:
      - dict → return a shallow copy (never mutate the original)
      - valid JSON string that parses to dict → return parsed dict
      - None, empty string, non-dict JSON, or invalid JSON → return {}
    """
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            logger.warning("payload_json normalization failed: type=%s", type(value).__name__)
            return {}
    return {}


# 模块定位:JSON 工具集(LLM 响应抽取 + MySQL JSON 列归一化)
#
# 提供:
#   - extract_json_from_llm_response(text)
#     处理 Markdown 代码块、```json 围栏、bracket-matched、首尾花括号；
#   - normalize_json_object(value)
#     aiomysql 把 JSON 列 decode 成 dict;str/None 走 fast path。
#
# 链路:
#   TestPlanGeneratorTool 等调 LLM → 拿到 raw text →
#     extract_json_from_llm_response() → dict → ResultParser.parse_and_validate
#
# 关键约束:
#   - **不**调第三方 JSON parser(regex-based,无依赖);
#   - 不嵌套 schema 验证(这是 ResultParser 的事);
#   - 不进 LLM input,只用于 output 解析。
