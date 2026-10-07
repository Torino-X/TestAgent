"""Prompt Injection 防逃逸：Trust 标签 + 内容 neutralize。

CE-02 WP-3：
- 外部来源（file/kb/memory/tool_output）打 trust=untrusted_reference 标签；
- Content/metadata 转义 + 防闭合标签（``</context-section>`` / ``<system`` /
  ``ignore previous instructions`` 等封闭/注入序列被 neutralize）；
- Composer 消费；Trust 校验（禁止 Memory/KB/File 提升为 trusted_instruction）。
"""

from __future__ import annotations

import re

from app.context_engine.models.context import ContextItem
from app.context_engine.models.enums import ContextTrust, SourceType

# 注入 / 闭合序列（neutralize 目标）
_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"</\s*context-section\s*>", re.IGNORECASE),
    re.compile(r"<\s*system\b", re.IGNORECASE),
    re.compile(r"<\s*/?\s*system\s*>", re.IGNORECASE),
    re.compile(r"<\s*/?\s*context-section\s*>", re.IGNORECASE),
    re.compile(r"<\s*context-section\b", re.IGNORECASE),
]

_PHRASE_PATTERNS: list[re.Pattern] = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?)", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|prior)\s+instructions?", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?(previous|prior)\s+instructions?", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+", re.IGNORECASE),
    re.compile(r"new\s+system\s+prompt", re.IGNORECASE),
]

_UNTRUSTED_SOURCE_TYPES = {
    SourceType.FILE_SUMMARY,
    SourceType.PARSED_DOCUMENT,
    SourceType.TEMPLATE_SECTION,
    SourceType.ARTIFACT,
    SourceType.TOOL_OUTPUT,
    SourceType.KNOWLEDGE,
    SourceType.USER_MEMORY,
    SourceType.WORKSPACE_MEMORY,
}

_NEUTRALIZE_MARK = "[potential-injection-neutralized]"


def neutralize_text(text: str) -> str:
    """对内容做中性化处理：闭合标签 / 伪 system 序列被替换为安全标记。"""
    result = text or ""
    for pattern in _INJECTION_PATTERNS:
        result = pattern.sub(_NEUTRALIZE_MARK, result)
    for pattern in _PHRASE_PATTERNS:
        result = pattern.sub(_NEUTRALIZE_MARK, result)
    return result


def is_untrusted_source(source_type) -> bool:
    st = source_type if isinstance(source_type, SourceType) else SourceType(source_type)
    return st in _UNTRUSTED_SOURCE_TYPES


def tag_prompt_injection(items: list[ContextItem]) -> list[ContextItem]:
    """给外部来源打 trust=untrusted_reference 标签 + neutralize 内容。"""
    tagged: list[ContextItem] = []
    for it in items:
        source_type = it.source_type
        untrusted = is_untrusted_source(source_type)
        if untrusted:
            # 外部来源强制 untrusted（禁止提升为 trusted_instruction）
            metadata = dict(it.metadata)
            metadata["original_trust"] = it.trust.value if hasattr(it.trust, "value") else str(it.trust)
            tagged.append(
                it.model_copy(
                    update={
                        "trust": ContextTrust.UNTRUSTED_REFERENCE,
                        "content": neutralize_text(it.content),
                        "metadata": metadata,
                    }
                )
            )
        else:
            tagged.append(it)
    return tagged
# auto-appended module-level note: injection filter: 二次过滤检索内容里的潜在 prompt injection。
