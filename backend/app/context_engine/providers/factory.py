"""Provider Factory — 按 ModelConfig 能力类型实例化 Provider。

流程：ModelConfig(ORM) → 解密 api_key → ModelCapability(Mapper) →
Factory 按 capability_type 构造 Adapter。明文 Key 只在本 Factory 内
短暂存在，不写入 ModelCapability / Mapper / 日志 / State / Snapshot。

业务代码不写死任何公网厂商（base_url + api_key + model_name 统一配置）。
未来公司模型通过替换 base_url/api_key/model_name 或切换 Adapter 接入。
"""

from __future__ import annotations

import logging
from typing import Any

from app.context_engine.adapters.mapper import ModelCapability
from app.context_engine.errors import (
    ContextEngineError,
    ContextEngineFailure,
    ContextEngineStage,
)
from app.context_engine.providers.openai_compatible import (
    AlibabaModelStudioRerankerProvider,
    OpenAICompatibleEmbeddingProvider,
    JinaCohereStyleRerankerProvider,
)
from app.context_engine.providers.protocols import (
    EmbeddingProviderProtocol,
    ProviderFactoryProtocol,
    RerankerProviderProtocol,
)

logger = logging.getLogger(__name__)


def _raise_failure(error: ContextEngineError) -> None:
    """将 ContextEngineError（值对象）包装为可抛异常。"""
    raise ContextEngineFailure(error) from None


class ProviderFactory(ProviderFactoryProtocol):
    """ModelConfig → Provider Adapter 工厂。"""

    def get_embedding_provider(
        self, capability: ModelCapability, api_key: str
    ) -> EmbeddingProviderProtocol:
        if capability.capability_type not in ("embedding", "chat", "reasoning"):
            _raise_failure(
                ContextEngineError(
                    code="context.provider.mismatch",
                    detail=f"capability_type={capability.capability_type!r} 不能用于 Embedding",
                    stage=ContextEngineStage.RETRIEVAL,
                    retryable=False,
                    recoverable=True,
                )
            )
        return OpenAICompatibleEmbeddingProvider(capability, api_key)

    def get_reranker_provider(
        self, capability: ModelCapability, api_key: str
    ) -> RerankerProviderProtocol:
        if capability.capability_type not in ("reranker",):
            _raise_failure(
                ContextEngineError(
                    code="context.provider.mismatch",
                    detail=f"capability_type={capability.capability_type!r} 不能用于 Reranker",
                    stage=ContextEngineStage.RERANK,
                    retryable=False,
                    recoverable=True,
                )
            )
        if capability.provider == "alibaba-model-studio":
            return AlibabaModelStudioRerankerProvider(capability, api_key)
        return JinaCohereStyleRerankerProvider(capability, api_key)

    def create_chat_client(self, capability: ModelCapability, api_key: str) -> Any:
        """构造与现有 ``LLMClient`` 兼容的 config provider。

        复用现有 SettingsService.LLMConfigProvider 契约（api_url / api_key /
        model_name / timeout / enable_thinking），避免重复实现。
        """
        from app.services.settings_service import LLMConfigProvider

        return LLMConfigProvider(
            api_url=capability.api_base_url,
            api_key=api_key,
            model_name=capability.model_name,
            timeout=capability.timeout_seconds,
            enable_thinking=capability.enable_thinking,
        )


_default_factory = ProviderFactory()


def get_provider_factory() -> ProviderFactory:
    return _default_factory
# auto-appended module-level note: provider factory: 按 model name 选 OpenAI-compatible / 三方 / mock provider。
