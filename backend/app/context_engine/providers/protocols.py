"""Provider Protocol — Chat / Embedding / Reranker 能力契约。

设计文档 08 §24 / §28：代码通过 Adapter 对接 Provider，不绑定单一公网厂商。
业务层只依赖这些 Protocol；Provider Factory 从 ModelConfig 解析具体实现。

``capability_type`` 支持：chat / reasoning / embedding / reranker /
compression / memory_extraction。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.context_engine.adapters.mapper import ModelCapability


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ── Embedding DTO ────────────────────────────────────────────────────


class EmbeddingText(FrozenModel):
    text_id: str
    text: str


class EmbeddingRequest(FrozenModel):
    request_id: str
    model: str
    input_type: str = "document"  # query | document
    texts: tuple[EmbeddingText, ...]
    dimension: int = Field(ge=1, le=65536)
    normalize: bool = True
    instruction: str | None = None


class EmbeddingVector:
    """向量只在 Runtime / Index Worker 内部流转。

    不进入 MySQL / 日志 / REST / State（设计文档 08 §23.3）。
    """

    __slots__ = ("text_id", "values", "dimension", "normalized")

    def __init__(self, text_id: str, values: list[float], dimension: int, normalized: bool) -> None:
        self.text_id = text_id
        self.values = values
        self.dimension = dimension
        self.normalized = normalized


class EmbeddingResult:
    __slots__ = ("request_id", "model", "dimension", "vectors", "latency_ms")

    def __init__(self, request_id: str, model: str, dimension: int, vectors: list[EmbeddingVector], latency_ms: int) -> None:
        self.request_id = request_id
        self.model = model
        self.dimension = dimension
        self.vectors = vectors
        self.latency_ms = latency_ms


@runtime_checkable
class EmbeddingProviderProtocol(Protocol):
    """Embedding Provider 契约。"""

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult: ...

    def health_check(self) -> bool: ...


# ── Reranker DTO ─────────────────────────────────────────────────────


class RerankItem(FrozenModel):
    doc_id: str
    text: str


class RerankRequest(FrozenModel):
    request_id: str
    model: str
    query: str
    items: tuple[RerankItem, ...]
    instruction: str | None = None
    top_n: int | None = None


class RerankScore(FrozenModel):
    doc_id: str
    score: float


class RerankResult(FrozenModel):
    request_id: str
    model: str
    scores: list[RerankScore]
    latency_ms: int = 0


@runtime_checkable
class RerankerProviderProtocol(Protocol):
    """Reranker Provider 契约。"""

    async def rerank(self, request: RerankRequest) -> RerankResult: ...

    def health_check(self) -> bool: ...


@runtime_checkable
class RerankServiceProtocol(Protocol):
    """重排服务契约（比 Provider 更高一层）。

    业务层只依赖此协议；具体 Reranker Provider（公司或公网）通过
    Factory 注入。无可用 Reranker 时使用 Weighted RRF 并记录 fallback
    （守禁令 23 / 24）。
    """

    async def rerank(self, request: RerankRequest) -> RerankResult: ...

    @property
    def enabled(self) -> bool: ...


# ── Chat Provider ────────────────────────────────────────────────────


@runtime_checkable
class ChatProviderProtocol(Protocol):
    """Chat / Reasoning Provider 契约。

    ``config_provider`` 保持与现有 ``LLMClient`` 兼容：调用方传
    ``ModelCapability`` 或 ``LLMConfigProvider``。
    """

    def create_client(self, config: ModelCapability | Any) -> Any: ...


# ── Provider Registry / Factory 依赖 ────────────────────────────────


@runtime_checkable
class ProviderFactoryProtocol(Protocol):
    """Provider Factory 契约：按能力类型实例化对应 Provider。"""

    def get_embedding_provider(self, capability: ModelCapability) -> EmbeddingProviderProtocol: ...

    def get_reranker_provider(self, capability: ModelCapability) -> RerankerProviderProtocol: ...

    def create_chat_client(self, capability: ModelCapability) -> Any: ...
# auto-appended module-level note: provider 协议接口 (LLMClient 抽象 + structured output)。
