"""Source Adapter 基类：token 估算 + session 获取辅助。

CE-02 WP-2：adapter 之间共享的最小工具，不引入重抽象。
"""

from __future__ import annotations


class _TokenEstimateMixin:
    """token 估算 mixin：优先 TokenCounter，否则启发式兜底。"""

    _token_counter = None

    def _estimate(self, text: str) -> int:
        if self._token_counter is not None:
            return self._token_counter.estimate(text).tokens
        return max(1, len(text) // 3)
# auto-appended module-level note: BaseSource 抽象基类。
