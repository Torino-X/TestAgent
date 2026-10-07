"""CE-05 PII Redactor — 唯一 PII 规则入口。

CONTEXT_PII_MODE ∈ redact|mask|block（默认 redact）。
作用于 Memory 写入 / Payload put / Audit 读取 / 日志 四类写入点（§3.3 矩阵）。
"""

from __future__ import annotations

import os
import re

# 可配置 PII 检测 regex（默认集合：手机号/邮箱/身份证/银行卡/IP）
_DEFAULT_PII_PATTERNS = [
    # 中国大陆手机号
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    # 邮箱
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    # 身份证（18 位，含 X）
    re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"),
    # 银行卡（13-19 位连续数字）
    re.compile(r"(?<!\d)\d{13,19}(?!\d)"),
]


class PIIRedactor:
    """PII 检测与掩码统一入口（redact|mask|block 三态）。"""

    def __init__(self, mode: str | None = None) -> None:
        self._mode = self._resolve_mode(mode)

    @staticmethod
    def _resolve_mode(mode: str | None) -> str:
        if mode and mode in {"redact", "mask", "block"}:
            return mode
        return os.environ.get("CONTEXT_PII_MODE", "redact").strip().lower() or "redact"

    @property
    def mode(self) -> str:
        return self._mode

    def contains_pii(self, text: str) -> bool:
        return any(p.search(text or "") for p in _DEFAULT_PII_PATTERNS)

    def redact(self, text: str) -> str:
        """替换 PII 为 [redacted]。"""
        result = text or ""
        for pattern in _DEFAULT_PII_PATTERNS:
            result = pattern.sub("[redacted]", result)
        return result

    def mask(self, text: str) -> str:
        """掩码：保留首尾字符，中间 ***。"""
        result = text or ""
        for pattern in _DEFAULT_PII_PATTERNS:
            def _mask_match(m: re.Match) -> str:
                value = m.group(0)
                if len(value) <= 4:
                    return "***"
                return value[:2] + "***" + value[-2:]
            result = pattern.sub(_mask_match, result)
        return result

    def block(self, text: str) -> str:
        """block 模式：存在 PII 时抛出（调用方转 400 context.pii.blocked）。"""
        if self.contains_pii(text):
            raise PIIBlockedError("内容包含 PII，已按 block 模式拒绝")
        return text or ""

    def apply(self, text: str) -> str:
        """按当前模式处理文本。"""
        if self._mode == "redact":
            return self.redact(text)
        if self._mode == "mask":
            return self.mask(text)
        # block
        return self.block(text)


class PIIBlockedError(Exception):
    """block 模式拒绝（映射 400 context.pii.blocked）。"""


__all__ = ["PIIRedactor", "PIIBlockedError"]
# auto-appended module-level note: PII 屏蔽: email / 手机号 / 身份证 自动 redact。
