"""确定性 Chunking：RecursiveCharChunker。

CE-03 WP-3：chunk_policy_key = "recursive_char:v1"（组合 key 单列承载）。
确定性（同输入同输出）、保留章节边界（section_path 方案 A：作为 Chunk 内容
前缀 + 写入外部 payload）。产出 normalized_content（空白折叠 + 小写）与
content_hash（SHA-256）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

CHUNK_POLICY_KEY = "recursive_char:v1"


@dataclass(frozen=True)
class Chunk:
    """单个 Chunk（不含 DB 列依赖）。"""

    chunk_index: int
    content: str
    normalized_content: str
    content_hash: str
    char_count: int
    estimated_tokens: int
    section_path: str | None = None


def normalize_text(text: str) -> str:
    """空白折叠 + 小写（与 selection/dedup 同语义）。"""
    return re.sub(r"\s+", " ", text).strip().lower()


def compute_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RecursiveCharChunker:
    """确定性递归字符分块。

    - max_chars≈1200、overlap≈200；
    - 优先在段落边界（``\\n\\n``）拆分，其次句子边界（``。！？.!\n``），
      最后硬切 max_chars；
    - 每个 chunk 的 content 以 ``<section:path>`` 前缀（方案 A）注入，
      normalized 与 hash 基于带前缀内容；
    - chunk_policy_key = "recursive_char:v1"。
    """

    def __init__(
        self,
        *,
        max_chars: int = 1200,
        overlap: int = 200,
        section_path: str | None = None,
    ) -> None:
        if max_chars <= overlap:
            raise ValueError("max_chars must be greater than overlap")
        self._max_chars = max_chars
        self._overlap = overlap
        self._section_path = section_path

    def chunk(self, text: str) -> list[Chunk]:
        prefix = f"<section:{self._section_path}>" if self._section_path else ""
        content = text.strip()
        if not content:
            return []

        # 先在段落/句子边界预切，再逐段应用滑动窗口
        segments = self._split_on_boundaries(content)
        windows: list[str] = []
        for seg in segments:
            windows.extend(self._window(seg))

        chunks: list[Chunk] = []
        for i, window in enumerate(windows):
            body = window.strip()
            if not body:
                continue
            full = f"{prefix}{body}" if prefix else body
            norm = normalize_text(full)
            chunks.append(
                Chunk(
                    chunk_index=i,
                    content=full,
                    normalized_content=norm,
                    content_hash=compute_sha256(full),
                    char_count=len(full),
                    estimated_tokens=max(1, len(full) // 3),
                    section_path=self._section_path,
                )
            )
        return chunks

    def _split_on_boundaries(self, text: str) -> list[str]:
        # 优先段落
        parts = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
        segments: list[str] = []
        for part in parts:
            if len(part) <= self._max_chars:
                segments.append(part)
            else:
                segments.extend(
                    [s for s in re.split(r"(?<=[。！？.!?；;])\s*", part) if s.strip()]
                    or [part]
                )
        return segments

    def _window(self, text: str) -> list[str]:
        out: list[str] = []
        start = 0
        n = len(text)
        if n == 0:
            return out
        while start < n:
            end = min(start + self._max_chars, n)
            if end < n:
                # 尝试在最近的句子/空白边界回退
                candidate = text.rfind("。", start, end)
                candidate = max(candidate, text.rfind(" ", start, end), text.rfind("\n", start, end))
                if candidate > start + self._max_chars // 2:
                    end = candidate + 1
            out.append(text[start:end].strip())
            if end >= n:
                break
            start = max(end - self._overlap, start + 1)
        return [w for w in out if w]
# auto-appended module-level note: chunker: 文档切片(text → chunks), 控制 overlap 与 token 上限。
