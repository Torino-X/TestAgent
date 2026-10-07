"""CE-05 Injection Guard — 唯一 Prompt Injection 规则入口。

委托 app.context_engine.selection.injection_filter（既有 neutralize/tag），
不保留第二套规则。
"""

from __future__ import annotations

from typing import Any


class InjectionGuard:
    """Prompt Injection 检测/中性化统一入口。"""

    def __init__(self) -> None:
        from app.context_engine.selection.injection_filter import (
            neutralize_text,
            tag_prompt_injection,
        )

        self._neutralize_text = neutralize_text
        self._tag_prompt_injection = tag_prompt_injection

    def neutralize(self, text: str) -> str:
        """对内容做中性化处理（闭合标签/伪 system 序列 → 安全标记）。"""
        return self._neutralize_text(text)

    def tag_items(self, items: list[Any]) -> list[Any]:
        """给外部来源打 untrusted 标签 + neutralize（委托既有实现）。"""
        return self._tag_prompt_injection(items)

    @staticmethod
    def sanitize(text: str) -> str:
        """零实例静态便捷入口（无状态，线程安全）。"""
        from app.context_engine.selection.injection_filter import neutralize_text

        return neutralize_text(text)


__all__ = ["InjectionGuard"]
# auto-appended module-level note: prompt injection 检测: 拦截 user 文本里的越权指令。
