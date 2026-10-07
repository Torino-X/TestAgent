"""Memory Extraction 递归保护（CE-03 WP-8）。

typed recursion guard：提取深度 > 1 → 拒绝（context.memory.extract.depth_exceeded）。
禁止 Memory Source（防"用已有记忆提取新记忆"）、禁止二次 Auto Extract、
禁止 Auto Activate（提取产物只写 candidate）。
"""

from __future__ import annotations

MAX_EXTRACT_DEPTH = 1


class ExtractionDepthExceeded(Exception):
    """提取深度超限。"""

    code = "context.memory.extract.depth_exceeded"


class MemoryExtractionGuard:
    """提取前置校验。"""

    def __init__(self, *, max_depth: int = MAX_EXTRACT_DEPTH) -> None:
        self._max_depth = max_depth

    def check(self, *, current_depth: int, source_has_memory: bool = False) -> None:
        if current_depth > self._max_depth:
            raise ExtractionDepthExceeded()
        if source_has_memory:
            # 禁止用已有记忆提取新记忆（Memory Source 排除）
            raise ExtractionDepthExceeded()
# auto-appended module-level note: extraction: LLM 抽取 user message 里的 user memory / project rule。
