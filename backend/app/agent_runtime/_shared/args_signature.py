"""args_signature — stable 12-char SHA prefix of tool arguments (Phase 2.4 shared).

Rule 11 (no raw LLM text / CoT in audit): 仅保留 decision_summary + tool_name +
12-char SHA prefix of args 用于审计。

Phase 2.3 implementation 在 ``preparation/prompt.py:171-176``;
Phase 2.4 抽出到 ``_shared/args_signature.py`` 以便 Repair Agent 复用。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional


def args_signature(args: Optional[Dict[str, Any]]) -> str:
    """Stable 12-char SHA prefix of tool arguments for audit (Rule 11)."""
    if not args:
        return ""
    canonical = json.dumps(args, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


__all__ = ["args_signature"]

# 模块定位:args_signature = tool args 的稳定 12-char SHA 前缀(审计占位用)
#
# Rule 11 (no raw LLM text / CoT in audit): 审计日志/事件流严禁保存 LLM 原始
# 输出与 chain-of-thought,只允许保存:
#   - decision_summary(人读 1 行)
#   - tool_name
#   - args_signature(本模块)
#
# 调用方:preparation/decision_filter.py, repair/decision_filter.py, 所有
# 写 ToolCall / RepairDecision 行时,把 args serialize 后 hash 取前 12 字符。
#
# 关键约束:
#   - hash 算法必须稳定(SHA-1 / SHA-256 之一均可),且对 {None, {}, [], 0}
#     一致处理;
#   - 不允许写 args 的原始值;
#   - 跨会话同一 args 产生同一 signature(便于审计聚合)。
