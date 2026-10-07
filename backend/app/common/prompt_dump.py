"""Prompt dump utility — adapted from legacy prompt_dump.py.

Saves full prompts to disk for debugging, controlled by the environment
variable ``PLANWISE_DUMP_PROMPTS=1``.  API keys are sanitised before writing.

Equivalent migration: all capabilities preserved.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

DUMP_DIR = Path("output") / "prompt_dumps"

# Patterns that look like API keys (sk-…, das-…, etc.)
_KEY_PATTERNS = [
    re.compile(r"(sk-[a-zA-Z0-9_-]{20,})"),
    re.compile(r"(das-[a-zA-Z0-9_-]{20,})"),
    re.compile(r"(Bearer\s+)([a-zA-Z0-9_-]{20,})"),
    re.compile(r"(api_key[=:]\s*)([a-zA-Z0-9_-]{16,})", re.IGNORECASE),
    re.compile(r"(api-key[=:]\s*)([a-zA-Z0-9_-]{16,})", re.IGNORECASE),
]


def _is_enabled() -> bool:
    return os.getenv("PLANWISE_DUMP_PROMPTS", "").strip() == "1"


def _ensure_dir() -> Path:
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    return DUMP_DIR


def _sanitize(text: str) -> str:
    """Replace API keys and tokens with ``****REDACTED****``."""
    for pattern in _KEY_PATTERNS:
        text = pattern.sub(r"****REDACTED****", text)
    return text


def dump_prompt(prompt_text: str, context: str = "") -> str:
    """Save a prompt to disk, returning the file path (empty string if disabled).

    Args:
        prompt_text: The full prompt text (will be sanitised).
        context: An identifier such as ``"main"`` or ``"vision_img3"``.
    """
    if not _is_enabled():
        return ""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    stem = f"{timestamp}_{context}" if context else timestamp
    file_path = _ensure_dir() / f"{stem}.txt"
    file_path.write_text(_sanitize(prompt_text), encoding="utf-8")
    return str(file_path)


# 模块定位:Prompt dump 工具(从 legacy prompt_dump.py 迁移)
#
# 在 env `PLANWISE_DUMP_PROMPTS=1` 时把完整 prompt 落盘(便于调试)。
# API key 会先脱敏再写。
#
# 链路:
#   prompt_builder 构造完 prompt → prompt_dump.maybe_dump(prompt, role='system')
#     → 写 .tmp file 到本地调试目录
#
# 关键约束:
#   - 生产默认关闭(PLAWISE_DUMP_PROMPTS 不设);
#   - API key / API token / Bearer 等敏感字段必须 redact;
#   - 文件名带时间戳 + role + prompt_hash,避免 LLM provider 打爆磁盘;
#   - 仅 dev / staging 用,生产跑要清理由 oncall 决定。
