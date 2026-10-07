"""OpenAI 兼容 Chat/Embedding + Jina/Cohere 风格 Reranker Adapter 实现。

``base_url`` + ``api_key`` + ``model_name`` + ``capability_type`` 统一配置，
业务代码不写死任何公网厂商。当前实现：

- Chat / Reasoning：复用现有 ``LLMClient``（OpenAI 兼容 SDK）
- Embedding：OpenAI 兼容 /embeddings 接口
- Reranker：``/rerank`` 是 jina / cohere 风格接口，**不是 OpenAI 标准**。
  该类明确标注为非 OpenAI 通用实现，仅作为占位 Adapter；
  真实公网/公司 Reranker Provider 确定后由专用 Adapter 替换。

未来公司模型通过替换 ``base_url`` / ``api_key`` / ``model_name`` 或切换
Adapter 接入，不改业务代码。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.context_engine.adapters.mapper import ModelCapability
from app.context_engine.errors import (
    ContextEngineError,
    ContextEngineFailure,
    ContextEngineStage,
)
from app.context_engine.providers.protocols import (
    EmbeddingProviderProtocol,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingText,
    EmbeddingVector,
    RerankItem,
    RerankRequest,
    RerankResult,
    RerankScore,
    RerankerProviderProtocol,
)

logger = logging.getLogger(__name__)


def _raise_failure(error: ContextEngineError) -> None:
    raise ContextEngineFailure(error) from None


class OpenAICompatibleEmbeddingProvider(EmbeddingProviderProtocol):
    """OpenAI-compatible ``/embeddings`` 接口实现。

    ``api_key`` 由 Factory 注入（来自解密后的 ModelConfig），不存入
    ModelCapability / Mapper，绝不进日志 / State / Snapshot。
    """

    def __init__(self, capability: ModelCapability, api_key: str) -> None:
        self._capability = capability
        self._api_key = api_key
        self._timeout = httpx.Timeout(capability.timeout_seconds or 30)
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._capability.api_base_url and self._capability.model_name)

    def _get_client(self) -> httpx.AsyncClient:
        """复用单个 AsyncClient（HTTP keepalive + 连接池，避免每次重建连接）。"""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, limits=httpx.Limits(max_connections=10, max_keepalive_connections=10))
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        capability = self._capability
        if not self._api_key:
            _raise_failure(
                ContextEngineError(
                    code="context.provider.embedding.no_key",
                    detail="Embedding Provider 缺少 api_key",
                    stage=ContextEngineStage.RETRIEVAL,
                    retryable=False,
                    recoverable=True,
                )
            )
        base = capability.api_base_url.rstrip("/")
        payload: dict[str, Any] = {
            "model": request.model or capability.model_name,
            "input": [t.text for t in request.texts],
        }
        start = time.monotonic()
        try:
            client = self._get_client()
            resp = await client.post(
                f"{base}/embeddings",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            _raise_failure(
                ContextEngineError(
                    code="context.provider.embedding.unavailable",
                    detail=f"Embedding Provider 调用失败: {type(exc).__name__}",
                    stage=ContextEngineStage.RETRIEVAL,
                    retryable=True,
                    recoverable=True,
                )
            )

        vectors: list[EmbeddingVector] = []
        data = body.get("data", [])
        dimension = len(data[0]["embedding"]) if data else request.dimension
        for item in data:
            index = int(item.get("index", 0))
            text_id = request.texts[index].text_id if index < len(request.texts) else f"idx_{index}"
            vectors.append(
                EmbeddingVector(
                    text_id=text_id,
                    values=list(item.get("embedding", [])),
                    dimension=dimension,
                    normalized=request.normalize,
                )
            )
        return EmbeddingResult(
            request_id=request.request_id,
            model=request.model or capability.model_name,
            dimension=dimension,
            vectors=vectors,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    def health_check(self) -> bool:
        return bool(self._capability.api_base_url and self._capability.model_name)


class JinaCohereStyleRerankerProvider(RerankerProviderProtocol):
    """jina/cohere 风格 ``/rerank`` 接口占位实现。

    注意：``/rerank`` 不是 OpenAI 标准接口；真实公网/公司 Reranker
    Provider 确定后应由专用 Adapter 替换本类。
    ``api_key`` 由 Factory 注入，不存入 ModelCapability。
    """

    def __init__(self, capability: ModelCapability, api_key: str) -> None:
        self._capability = capability
        self._api_key = api_key
        self._timeout = httpx.Timeout(capability.timeout_seconds or 30)

    @property
    def enabled(self) -> bool:
        return bool(self._capability.api_base_url and self._capability.model_name)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def rerank(self, request: RerankRequest) -> RerankResult:
        capability = self._capability
        if not self._api_key:
            _raise_failure(
                ContextEngineError(
                    code="context.provider.rerank.no_key",
                    detail="Reranker Provider 缺少 api_key",
                    stage=ContextEngineStage.RERANK,
                    retryable=False,
                    recoverable=True,
                )
            )
        base = capability.api_base_url.rstrip("/")
        payload: dict[str, Any] = {
            "model": request.model or capability.model_name,
            "query": request.query,
            "documents": [i.text for i in request.items],
        }
        if request.top_n:
            payload["top_n"] = request.top_n
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{base}/rerank",
                    headers=self._headers(),
                    json=payload,
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as exc:
            _raise_failure(
                ContextEngineError(
                    code="context.provider.rerank.unavailable",
                    detail=f"Reranker Provider 调用失败: {type(exc).__name__}",
                    stage=ContextEngineStage.RERANK,
                    retryable=True,
                    recoverable=True,
                )
            )

        scores: list[RerankScore] = []
        results = body.get("results", body.get("data", []))
        for item in results:
            if isinstance(item, dict):
                doc_id = str(item.get("document_id", item.get("index", "")))
                score = float(item.get("relevance_score", item.get("score", 0.0)))
            else:
                doc_id = ""
                score = 0.0
            scores.append(RerankScore(doc_id=doc_id, score=score))
        return RerankResult(
            request_id=request.request_id,
            model=request.model or capability.model_name,
            scores=scores,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    def health_check(self) -> bool:
        return bool(self._capability.api_base_url and self._capability.model_name)


class AlibabaModelStudioRerankerProvider(RerankerProviderProtocol):
    """Alibaba Model Studio text-rerank adapter.

    Model Studio supplies a complete service endpoint and returns ranked
    document positions under ``output.results``. It is not compatible with
    the Jina/Cohere ``<base>/rerank`` URL or top-level ``results`` payload.
    """

    def __init__(self, capability: ModelCapability, api_key: str) -> None:
        self._capability = capability
        self._api_key = api_key
        self._timeout = httpx.Timeout(capability.timeout_seconds or 30)

    @property
    def enabled(self) -> bool:
        return bool(self._capability.api_base_url and self._capability.model_name)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def rerank(self, request: RerankRequest) -> RerankResult:
        capability = self._capability
        if not self._api_key:
            _raise_failure(
                ContextEngineError(
                    code="context.provider.rerank.no_key",
                    detail="Reranker Provider 缺少 api_key",
                    stage=ContextEngineStage.RERANK,
                    retryable=False,
                    recoverable=True,
                )
            )
        payload: dict[str, Any] = {
            "model": request.model or capability.model_name,
            "query": request.query,
            "documents": [item.text for item in request.items],
        }
        if request.top_n:
            payload["top_n"] = request.top_n
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    capability.api_base_url.rstrip("/"),
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            _raise_failure(
                ContextEngineError(
                    code="context.provider.rerank.unavailable",
                    detail=f"Reranker Provider 调用失败: {type(exc).__name__}",
                    stage=ContextEngineStage.RERANK,
                    retryable=True,
                    recoverable=True,
                )
            )

        output = body.get("output") if isinstance(body, dict) else None
        results = output.get("results") if isinstance(output, dict) else None
        if not isinstance(results, list):
            _raise_failure(
                ContextEngineError(
                    code="context.provider.rerank.invalid_response",
                    detail="Alibaba Reranker 响应未包含 output.results",
                    stage=ContextEngineStage.RERANK,
                    retryable=False,
                    recoverable=True,
                )
            )

        scores: list[RerankScore] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            if not isinstance(index, int) or not 0 <= index < len(request.items):
                continue
            try:
                score = float(item.get("relevance_score", item.get("score", 0.0)))
            except (TypeError, ValueError):
                continue
            scores.append(RerankScore(doc_id=request.items[index].doc_id, score=score))
        return RerankResult(
            request_id=request.request_id,
            model=request.model or capability.model_name,
            scores=scores,
            latency_ms=int((time.monotonic() - start) * 1000),
        )


# ── 同步便捷方法（避免每次调用新建 client 的样板）────────────────────────


def make_embedding_provider(
    capability: ModelCapability, api_key: str
) -> OpenAICompatibleEmbeddingProvider:
    return OpenAICompatibleEmbeddingProvider(capability, api_key)


def make_reranker_provider(
    capability: ModelCapability, api_key: str
) -> RerankerProviderProtocol:
    if capability.provider == "alibaba-model-studio":
        return AlibabaModelStudioRerankerProvider(capability, api_key)
    return JinaCohereStyleRerankerProvider(capability, api_key)
# auto-appended module-level note: OpenAI-compatible provider adapter (兼容 vLLM / 阿里 / 自托管)。
