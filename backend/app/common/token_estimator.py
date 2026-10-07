"""Lightweight token estimator for context budget control.

Uses a heuristic: Chinese characters ~1 token per 1.5 chars,
English/other characters ~1 token per 4 chars.  For mixed text
the two counts are summed, which is conservative (overestimates
slightly) — safe for budget enforcement.
"""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Estimate the token count of *text* without a real tokenizer."""
    if not text:
        return 0
    chinese = 0
    for ch in text:
        if "一" <= ch <= "鿿":
            chinese += 1
    other = len(text) - chinese
    return int(chinese / 1.5 + other / 4)


# 模块定位:轻量级 token 估算器(上下文预算控制)
#
# 启发式规则:
#   - 中文 ~1.5 字符/token
#   - 英文/其它 ~4 字符/token
#   - 混合文本两者相加(保守高估,**不怕超量,怕低估**)
#
# 链路:
#   ConversationContextService / ChatLLMService 在拼接 prompt 前,
#   用本服务估算各 section 长度,触发 overflow 降级(自动截断 + warn)。
#
# 关键约束:
#   - 仅估算,不准 ——
#     真值以 model_configs.max_tokens 为准,这里只是预警;
#   - 绝不写入 prompt,只读;
#   - 在线服务快速路径(< 1ms per call);
#   - 不要替换为 tiktoken,沿用字符启发式(更跨模型稳定)。
