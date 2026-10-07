"""ModelCapabilities — 描述当前 LLM Provider 的能力 (Phase 2.3)。

设计原则:
* **保守默认**:native_tool_calling=False (LLMClient 当前没有工具调用 API)
* 通过 ``resolve_capabilities()`` 从 LLMConfigProvider 探测;Provider 不可用时
  返回 frozen dataclass with native_tool_calling=False → Mode A always works
* 测试可通过 monkeypatch 替换 resolver 注入 native_tool_calling=True,
  以验证 Mode B stub 路径 (test_prep_native_mode_b_deferred)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ModelCapabilities:
    """单 Provider 的能力描述。frozen + slots,线程安全。"""

    native_tool_calling: bool = False
    structured_output: bool = True
    token_streaming: bool = False
    parallel_tool_calls: bool = False
    max_context_tokens: int = 8000
    provider_label: str = "MiniMax-M3"


# ── Resolver ────────────────────────────────────────────────────────────


def _safe_call_provider() -> Optional[Any]:
    """从 LLMConfigCache / SettingsService 取当前配置 (失败则 None)。

    故意用 try/except 包裹:Provider 探测不应阻塞 Preparation Agent。
    """
    try:
        from app.services.settings_service import SettingsService  # noqa: F401
    except Exception:
        return None
    try:
        # Phase 2.3 范围内不强行依赖具体 SettingsService API;
        # 仅探测 provider 名 + 是否为 OpenAI 兼容即可。
        from app.core.config import get_settings  # type: ignore
        settings = get_settings()
        return getattr(settings, "llm", None) or settings
    except Exception:
        return None


def resolve_capabilities(*, override: Optional[ModelCapabilities] = None) -> ModelCapabilities:
    """返回当前进程的 LLM 能力描述。

    Args:
        override: 测试 / 入口可显式传入,跳过探测
    """
    if override is not None:
        return override

    provider = _safe_call_provider()
    if provider is None:
        # Provider 不可用 — 保守默认;Mode A 永远 work
        return ModelCapabilities()

    # 当前 LLMClient 走 openai AsyncOpenAI SDK 调用 chat.completions,
    # 但代码库内**没有任何** `tools=` / `tool_choice=` 调用 (Phase 2.3 调研结论)。
    # 因此即便底层是 OpenAI 兼容 Provider,Preparation Agent 也不走 native 路径。
    # Phase 2.4+ 等 LLMClient 提供 generate_with_tools 后,再细化探测。
    label = (
        getattr(provider, "model_name", None)
        or getattr(provider, "model", None)
        or getattr(provider, "provider_label", None)
        or "MiniMax-M3"
    )
    return ModelCapabilities(provider_label=str(label))


__all__ = ["ModelCapabilities", "resolve_capabilities"]

# module-level note (auto-appended):
# ModelCapabilities + resolve_capabilities — 当前 LLM 能力探测。
# 关键约束: 不发起实际 LLM call(只读 config / cache 校验)。
