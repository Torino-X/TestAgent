"""Sentinel returned when a user has no model configuration.

F015 — the system no longer falls back to a global default or
environment variables.  When ``build_llm_config_provider(user_id)``
finds no row for the given user, it returns an
``LLMNotConfiguredMarker`` instance so the calling code can produce a
clear, user-actionable error.

Design choice: we deliberately do NOT raise an exception here.  Raising
an ``AppError`` would force every LLM call site (intent router, chat
service, agent tool) to wrap the call in a try/except, complicating the
existing error contracts.  Returning a marker preserves the existing
``LLMConfigProvider`` duck-type (callers can read ``api_url`` etc. as
they always did); ``LLMClient._resolve_config`` then inspects
``is_unconfigured`` and converts the call into an ``LLMClientError``
with a friendly message that the frontend routes to the Settings page.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LLMNotConfiguredMarker:
    """A drop-in LLMConfigProvider that signals "user has no model".

    Satisfies the structural type ``LLMConfigProvider`` (it has the same
    fields plus the discriminator ``is_unconfigured=True``).  The
    ``get_effective_api_key`` method returns an empty string so any
    downstream code that calls it without checking the marker first
    will get a clear "empty key" error rather than a partially-populated
    config.
    """

    api_url: str = ""
    api_key: str = ""
    model_name: str = ""
    timeout: int = 0
    enable_thinking: bool = False
    is_unconfigured: bool = True

    def get_effective_api_key(self) -> str:
        return ""
# llm_not_configured:用户未配置模型时的 sentinel(LLMNotConfiguredMarker);由 LLMClient._resolve_config 转换为友好错误,引导前端跳转 Settings。
