"""CE-05 SafeExcerpt — 安全审计摘要统一入口。

委托 composer redaction 规则（_redact_user SHA-8 + MAX_PROMPT_EXCERPT_CHARS）。
只输出 metadata/digest/计数，不输出正文/secret/storage path。
"""

from __future__ import annotations

import hashlib

MAX_PROMPT_EXCERPT_CHARS = 512


def redact_user(user_id: str) -> str:
    """用户标识脱敏：SHA-256 前 8 位，不落明文 id。"""
    if not user_id:
        return "?"
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:8]


class SafeExcerpt:
    """安全审计摘要构建器（只含审计 metadata，不含正文/secret）。"""

    @staticmethod
    def build(
        *,
        call_site: str,
        user_id: str,
        node: str,
        agent: str,
        kinds: list[str],
        section_keys: list[str],
        included: int,
        dropped: int,
        tokens: int,
        thread_id: str,
        limit: int = MAX_PROMPT_EXCERPT_CHARS,
    ) -> str:
        parts = [
            f"call_site={call_site or '?'}",
            f"user={redact_user(user_id)}",
            f"node={node or '?'}",
            f"agent={agent or '?'}",
            f"types={','.join(kinds) or 'none'}",
            f"sections={','.join(str(s) for s in section_keys[:20]) or 'none'}",
            f"included={included}",
            f"dropped={dropped}",
            f"tokens={tokens}",
            f"thread={thread_id or '?'}",
        ]
        excerpt = " | ".join(parts)
        if len(excerpt) > limit:
            excerpt = excerpt[:limit] + "…"
        return excerpt


__all__ = ["SafeExcerpt", "redact_user", "MAX_PROMPT_EXCERPT_CHARS"]
# auto-appended module-level note: safe excerpt: 把长文本安全切段(避免 LLM prompt 截断 + 注入)。
