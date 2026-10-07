"""Dedup：三层去重。

CE-02 WP-3：
1) source_public_id + version；
2) content_hash（SHA-256）；
3) normalized text。
输出 included / dup-dropped。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.context_engine.models.context import ContextItem
from app.context_engine.models.selection import DroppedContextRef


def _normalize_text(text: str) -> str:
    """空白折叠 + 小写，用于文本级去重。"""
    text = text or ""
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


class ContextDeduplicator:
    """三层去重器。"""

    def __init__(self) -> None:
        self._seen_ids: set[tuple[str, str]] = set()
        # Content-level dedupe is intentionally scoped to a semantic kind.
        # A MEMORY learned from a conversation turn has the same text as that
        # turn by design, but it remains a separately auditable durable source
        # and must not be silently removed from the Memory section.
        self._seen_hashes: set[tuple[str, str]] = set()
        self._seen_texts: set[tuple[str, str]] = set()

    def dedup(self, items: list[ContextItem]) -> tuple[list[ContextItem], list[DroppedContextRef]]:
        """对 items 去重，返回 (included, dropped)。"""
        included: list[ContextItem] = []
        dropped: list[DroppedContextRef] = []
        for it in items:
            key = self._identity_key(it)
            if key is not None and key in self._seen_ids:
                dropped.append(DroppedContextRef(item_id=it.item_id, reason="duplicate", detail="source_ref+version 重复"))
                continue
            if key is not None:
                self._seen_ids.add(key)

            kind_key = getattr(it.kind, "value", str(it.kind))
            content_hash = _content_hash(it.content)
            hash_key = (kind_key, content_hash)
            if content_hash and hash_key in self._seen_hashes:
                dropped.append(DroppedContextRef(item_id=it.item_id, reason="duplicate", detail="content_hash 重复"))
                continue
            if content_hash:
                self._seen_hashes.add(hash_key)

            norm = _normalize_text(it.content)
            text_key = (kind_key, norm)
            if norm and text_key in self._seen_texts:
                dropped.append(DroppedContextRef(item_id=it.item_id, reason="duplicate", detail="normalized text 重复"))
                continue
            if norm:
                self._seen_texts.add(text_key)

            included.append(it)
        return included, dropped

    def reset(self) -> None:
        self._seen_ids.clear()
        self._seen_hashes.clear()
        self._seen_texts.clear()

    @staticmethod
    def _identity_key(item: ContextItem) -> tuple[str, str] | None:
        if not item.source_ref:
            return None
        version = str(item.metadata.get("version") or item.metadata.get("version_no") or "")
        return (item.source_ref, version)


def _content_hash(content: str) -> str:
    if not content:
        return ""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
# auto-appended module-level note: dedup: 同 content_hash 去重, 同 doc 多 chunk 合并。
