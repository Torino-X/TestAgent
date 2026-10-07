"""Context Engine Provider 层。"""

from app.context_engine.providers.factory import ProviderFactory, get_provider_factory
from app.context_engine.providers.fakes import DisabledReranker, FakeReranker
from app.context_engine.providers.openai_compatible import (
    AlibabaModelStudioRerankerProvider,
    OpenAICompatibleEmbeddingProvider,
    JinaCohereStyleRerankerProvider,
    make_embedding_provider,
    make_reranker_provider,
)
from app.context_engine.providers.protocols import (
    ChatProviderProtocol,
    EmbeddingProviderProtocol,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingText,
    EmbeddingVector,
    ProviderFactoryProtocol,
    RerankItem,
    RerankRequest,
    RerankResult,
    RerankScore,
    RerankerProviderProtocol,
)

__all__ = [
    "ProviderFactory",
    "get_provider_factory",
    "OpenAICompatibleEmbeddingProvider",
    "AlibabaModelStudioRerankerProvider",
    "JinaCohereStyleRerankerProvider",
    "make_embedding_provider",
    "make_reranker_provider",
    "ChatProviderProtocol",
    "EmbeddingProviderProtocol",
    "EmbeddingRequest",
    "EmbeddingResult",
    "EmbeddingText",
    "EmbeddingVector",
    "ProviderFactoryProtocol",
    "RerankItem",
    "RerankRequest",
    "RerankResult",
    "RerankScore",
    "RerankerProviderProtocol",
]
# auto-appended module-level note: providers 子包: LLM Provider adapter + factory 注册入口。
