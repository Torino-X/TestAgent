"""Context Selection 层：selector / dedup / quota / injection_filter。"""

from app.context_engine.selection.dedup import ContextDeduplicator
from app.context_engine.selection.injection_filter import (
    is_untrusted_source,
    neutralize_text,
    tag_prompt_injection,
)
from app.context_engine.selection.quota import SourceQuotaEnforcer, SourceQuotaPolicy
from app.context_engine.selection.selector import ContextSelector, SelectionConfig

__all__ = [
    "ContextDeduplicator",
    "is_untrusted_source",
    "neutralize_text",
    "tag_prompt_injection",
    "SourceQuotaEnforcer",
    "SourceQuotaPolicy",
    "ContextSelector",
    "SelectionConfig",
]
# auto-appended module-level note: selection 子包: 检索结果二次选择(去重 / 配额 / 注入过滤)。
