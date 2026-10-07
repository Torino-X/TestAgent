"""CE-01 Provider 层单元测试。

覆盖：ProviderFactory 能力匹配、Embedding/Reranker Adapter（httpx Mock）、
Key 安全（ModelCapability 不携带明文）、chat client 复用 LLMConfigProvider。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.context_engine.adapters import ModelCapability
from app.context_engine.errors import ContextEngineFailure
from app.context_engine.providers import (
    AlibabaModelStudioRerankerProvider,
    OpenAICompatibleEmbeddingProvider,
    JinaCohereStyleRerankerProvider,
    FakeReranker,
    DisabledReranker,
    ProviderFactory,
    EmbeddingRequest,
    EmbeddingText,
    RerankItem,
    RerankRequest,
)


def _capability(**overrides) -> ModelCapability:
    base = dict(
        public_id="mc_1",
        provider="openai-compatible",
        api_base_url="https://api.example.com/v1",
        model_name="model-x",
        capability_type="chat",
        timeout_seconds=30,
    )
    base.update(overrides)
    return ModelCapability(**base)


# ── Factory 能力匹配 ─────────────────────────────────────────────


def test_factory_embedding_provider():
    factory = ProviderFactory()
    provider = factory.get_embedding_provider(_capability(capability_type="embedding"), api_key="sk-1")
    assert isinstance(provider, OpenAICompatibleEmbeddingProvider)
    assert provider.health_check() is True


def test_factory_reranker_provider():
    factory = ProviderFactory()
    provider = factory.get_reranker_provider(_capability(capability_type="reranker"), api_key="sk-1")
    assert isinstance(provider, JinaCohereStyleRerankerProvider)


def test_factory_selects_alibaba_reranker_provider():
    factory = ProviderFactory()
    provider = factory.get_reranker_provider(
        _capability(provider="alibaba-model-studio", capability_type="reranker"),
        api_key="sk-1",
    )
    assert isinstance(provider, AlibabaModelStudioRerankerProvider)


def test_env_reranker_provider_is_enabled_without_exposing_key():
    from app.context_engine.providers.env_provider import build_env_reranker_provider

    provider, capability, api_key = build_env_reranker_provider(
        {
            "RERANKER_BASE_URL": "https://rerank.example/v1",
            "RERANKER_MODEL": "rerank-v1",
            "RERANKER_API_KEY": "secret-key",
            "RERANKER_PROVIDER": "alibaba-model-studio",
        }
    )

    assert isinstance(provider, AlibabaModelStudioRerankerProvider)
    assert provider.enabled is True
    assert capability.capability_type == "reranker"
    assert "secret-key" not in repr(capability)
    assert api_key == "secret-key"


def test_env_reranker_provider_detects_alibaba_model_studio_endpoint():
    from app.context_engine.providers.env_provider import build_env_reranker_provider

    provider, capability, _api_key = build_env_reranker_provider(
        {
            "RERANKER_BASE_URL": "https://example.maas.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
            "RERANKER_MODEL": "qwen3-vl-rerank",
            "RERANKER_API_KEY": "secret-key",
        }
    )

    assert capability.provider == "alibaba-model-studio"
    assert isinstance(provider, AlibabaModelStudioRerankerProvider)


def test_runtime_environment_reads_dotenv_without_exporting_secrets(monkeypatch):
    """CE external providers must see backend/.env values even when they are not exported.

    Pydantic Settings reads `.env` privately; provider/index factories used to read only
    ``os.environ`` and therefore silently built no Qdrant/ES/embedder in normal local
    startup.  The resolver keeps an explicit process environment override.
    """
    from app.context_engine.providers import runtime_env

    monkeypatch.setattr(
        runtime_env,
        "_read_dotenv_runtime_values",
        lambda: {
            "EMBEDDING_BASE_URL": "https://embedding.example/v1",
            "EMBEDDING_MODEL": "embedding-v1",
            "EMBEDDING_DIMENSION": "1024",
        },
    )
    monkeypatch.setenv("EMBEDDING_MODEL", "operator-override")

    resolved = runtime_env.get_context_engine_runtime_env()

    assert resolved["EMBEDDING_BASE_URL"] == "https://embedding.example/v1"
    assert resolved["EMBEDDING_DIMENSION"] == "1024"
    assert resolved["EMBEDDING_MODEL"] == "operator-override"


def test_factory_mismatch_raises_failure():
    factory = ProviderFactory()
    with pytest.raises(ContextEngineFailure) as exc_info:
        factory.get_reranker_provider(_capability(capability_type="embedding"), api_key="sk-1")
    assert exc_info.value.error.code == "context.provider.mismatch"


def test_factory_chat_client_reuses_llm_config_provider():
    factory = ProviderFactory()
    client = factory.create_chat_client(_capability(capability_type="chat", model_name="gpt-4o"), api_key="sk-1")
    assert client.model_name == "gpt-4o"
    assert client.api_url == "https://api.example.com/v1"


# ── Key 安全 ─────────────────────────────────────────────────────


def test_model_capability_never_carries_plaintext_key():
    cap = _capability()
    assert cap.api_key_masked == "****"
    assert "sk-1" not in repr(cap)


# ── Embedding Adapter ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_embedding_provider_calls_endpoint():
    provider = OpenAICompatibleEmbeddingProvider(_capability(capability_type="embedding"), api_key="sk-1")
    request = EmbeddingRequest(
        request_id="r1",
        model="text-embedding",
        texts=(EmbeddingText(text_id="t1", text="hello"),),
        dimension=8,
    )
    fake_response = {
        "data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]}]
    }
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_FakeResponse(fake_response))):
        result = await provider.embed(request)
    assert result.dimension == 8
    assert result.vectors[0].text_id == "t1"
    assert len(result.vectors[0].values) == 8


@pytest.mark.asyncio
async def test_embedding_provider_missing_key():
    provider = OpenAICompatibleEmbeddingProvider(_capability(capability_type="embedding"), api_key="")
    request = EmbeddingRequest(request_id="r1", model="m", texts=(), dimension=8)
    with pytest.raises(ContextEngineFailure) as exc_info:
        await provider.embed(request)
    assert exc_info.value.error.code == "context.provider.embedding.no_key"


@pytest.mark.asyncio
async def test_embedding_provider_http_error():
    provider = OpenAICompatibleEmbeddingProvider(_capability(capability_type="embedding"), api_key="sk-1")
    request = EmbeddingRequest(request_id="r1", model="m", texts=(), dimension=8)
    from httpx import ConnectError

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=ConnectError("boom"))):
        with pytest.raises(ContextEngineFailure) as exc_info:
            await provider.embed(request)
    assert exc_info.value.error.retryable is True


# ── Reranker Adapter ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reranker_provider_calls_endpoint():
    provider = JinaCohereStyleRerankerProvider(_capability(capability_type="reranker"), api_key="sk-1")
    request = RerankRequest(
        request_id="r1",
        model="rerank-model",
        query="登录模块",
        items=(RerankItem(doc_id="d1", text="账号锁定"),),
    )
    fake_response = {"results": [{"document_id": "d1", "relevance_score": 0.95}]}
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_FakeResponse(fake_response))):
        result = await provider.rerank(request)
    assert result.scores[0].doc_id == "d1"
    assert result.scores[0].score == 0.95


@pytest.mark.asyncio
async def test_alibaba_reranker_uses_configured_endpoint_and_maps_indices():
    provider = AlibabaModelStudioRerankerProvider(
        _capability(
            provider="alibaba-model-studio",
            capability_type="reranker",
            api_base_url="https://rerank.example/services/text-rerank",
        ),
        api_key="sk-1",
    )
    request = RerankRequest(
        request_id="r1",
        model="rerank-model",
        query="验证码规则",
        items=(
            RerankItem(doc_id="chunk-lock", text="账号锁定 30 分钟"),
            RerankItem(doc_id="chunk-sms", text="验证码 6 位数字"),
        ),
    )
    fake_response = {
        "output": {
            "results": [
                {"index": 1, "relevance_score": 0.95},
                {"index": 0, "relevance_score": 0.20},
            ]
        }
    }
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_FakeResponse(fake_response))) as post:
        result = await provider.rerank(request)

    assert post.await_args.args[0] == "https://rerank.example/services/text-rerank"
    assert [(score.doc_id, score.score) for score in result.scores] == [
        ("chunk-sms", 0.95),
        ("chunk-lock", 0.20),
    ]


@pytest.mark.asyncio
async def test_reranker_provider_missing_key():
    provider = JinaCohereStyleRerankerProvider(_capability(capability_type="reranker"), api_key="")
    request = RerankRequest(request_id="r1", model="m", query="q", items=())
    with pytest.raises(ContextEngineFailure) as exc_info:
        await provider.rerank(request)
    assert exc_info.value.error.code == "context.provider.rerank.no_key"


@pytest.mark.asyncio
async def test_executor_rerank_replaces_scores_and_order():
    from app.context_engine.providers.protocols import RerankResult, RerankScore
    from app.context_engine.retrieval.executor import RetrievalExecutor
    from app.context_engine.retrieval.retrieval import FusionResult

    class _Reranker:
        enabled = True

        async def rerank(self, request):
            return RerankResult(
                request_id=request.request_id,
                model=request.model,
                scores=[
                    RerankScore(doc_id="a", score=0.1),
                    RerankScore(doc_id="b", score=0.9),
                ]
            )

    fused = [
        FusionResult("a", "doc-a", 1, None, 1, 0.2, 0.2, None, "lexical"),
        FusionResult("b", "doc-b", 1, None, 2, 0.1, 0.1, None, "vector"),
    ]
    reranked = await RetrievalExecutor(rerank_service=_Reranker())._apply_rerank(
        type("_Request", (), {"query_text": "query"})(), fused
    )

    assert [(item.chunk_public_id, item.rerank_score, item.final_rank) for item in reranked] == [
        ("b", 0.9, 1),
        ("a", 0.1, 2),
    ]


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload
