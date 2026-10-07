"""内置 Model Capability Registry — 代码级静态注册表（不新建 DB 表）。

WP-BE-03：模型 context_window_tokens 解析优先级：
    1. User ModelConfig.context_window_tokens（永远最高）
    2. Backend Built-in Model Capability Registry（本模块）
    3. UNKNOWN（context_window_tokens=None）

禁止：
    - 未知模型默认 128K / 200K（通过 model_name 猜一个不受控值）
    - 用 max_tokens（输出生成上限）冒充 context_window_tokens

Registry key = (provider, normalized_model_name)，normalize 规则：小写 + 去首尾空白。
"""

from __future__ import annotations

# (provider, normalized_model_name) → context_window_tokens
# 数值为已知公开规格。未知模型 → None（不得伪造）。
_BUILTIN_MODEL_WINDOWS: dict[tuple[str, str], int] = {
    # OpenAI GPT-4 系列
    ("openai", "gpt-4o"): 128_000,
    ("openai", "gpt-4o-mini"): 128_000,
    ("openai", "gpt-4-turbo"): 128_000,
    ("openai", "gpt-4"): 8_192,
    ("openai", "gpt-3.5-turbo"): 16_385,
    # OpenAI o1 推理系列
    ("openai", "o1"): 200_000,
    ("openai", "o1-mini"): 128_000,
    ("openai", "o3"): 200_000,
    # Anthropic Claude 系列
    ("anthropic", "claude-3-5-sonnet"): 200_000,
    ("anthropic", "claude-3-5-sonnet-20240620"): 200_000,
    ("anthropic", "claude-3-5-haiku"): 200_000,
    ("anthropic", "claude-3-opus"): 200_000,
    ("anthropic", "claude-3-sonnet"): 200_000,
    ("anthropic", "claude-3-haiku"): 200_000,
    ("anthropic", "claude-2"): 100_000,
    # DeepSeek
    ("deepseek", "deepseek-chat"): 64_000,
    ("deepseek", "deepseek-reasoner"): 64_000,
    # 阿里云 DashScope（OpenAI 兼容）
    ("dashscope", "qwen-max"): 32_000,
    ("dashscope", "qwen-plus"): 32_000,
    ("dashscope", "qwen-turbo"): 8_000,
    ("dashscope", "qwen3.7-max"): 32_000,
    ("qwen", "qwen-max"): 32_000,
    ("qwen", "qwen-plus"): 32_000,
    ("qwen", "qwen-turbo"): 8_000,
    ("qwen", "qwen3.7-max"): 32_000,
    ("", "qwen3.7-max"): 32_000,
    # 智谱 GLM
    ("zhipu", "glm-4"): 128_000,
    ("zhipu", "glm-4-plus"): 128_000,
    ("zhipu", "glm-4-air"): 128_000,
    ("openai", "glm-4"): 128_000,
    ("openai", "glm-4-plus"): 128_000,
    # 百度文心（OpenAI 兼容 / ernie）
    ("ernie", "ernie-4.0-turbo"): 128_000,
    ("ernie", "ernie-3.5"): 8_000,
    ("openai", "ernie-4.0-turbo"): 128_000,
    # Mistral
    ("mistral", "mistral-large"): 128_000,
    ("mistral", "mistral-medium"): 32_000,
    ("mistral", "mistral-small"): 32_000,
}


def normalize_model_name(model_name: str) -> str:
    """规范化模型名：小写 + 去首尾空白（registry key 用）。"""
    return (model_name or "").strip().lower()


def resolve_builtin_window(provider: str | None, model_name: str | None) -> int | None:
    """按 (provider, normalized_model_name) 查询内置注册表。

    返回 None 表示未知（调用方不得假定 128K/200K）。
    """
    if not model_name:
        return None
    normalized = normalize_model_name(model_name)
    provider_key = (provider or "").strip().lower()
    # 优先精确 provider 匹配；其次允许 provider 无关的 model_name 匹配
    exact = _BUILTIN_MODEL_WINDOWS.get((provider_key, normalized))
    if exact is not None:
        return exact
    return _BUILTIN_MODEL_WINDOWS.get(("", normalized))


def builtin_registry_snapshot() -> dict[str, int]:
    """registry 快照（审计/诊断用，不暴露 provider 内网细节）。"""
    return {
        f"{provider}:{name}": window
        for (provider, name), window in sorted(_BUILTIN_MODEL_WINDOWS.items())
    }


__all__ = [
    "normalize_model_name",
    "resolve_builtin_window",
    "builtin_registry_snapshot",
]
# auto-appended module-level note: model 窗口注册表: 维护每模型的 context window / max output。
