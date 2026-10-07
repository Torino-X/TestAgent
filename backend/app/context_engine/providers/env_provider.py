"""从 .env 构造真实 Embedding / Reranker Provider（CE-03 E2E 阶段）。

Embedding 模型参数不与用户绑定（与主模型不同），临时经环境变量配置：
  EMBEDDING_BASE_URL    OpenAI 兼容 base_url（如 https://.../compatible-mode/v1）
  EMBEDDING_API_KEY     纯 ASCII API Key（仅本地 .env，不进日志/报告/State）
  EMBEDDING_MODEL       模型名（如 text-embedding-v3）
  EMBEDDING_DIMENSION   模型维度（不硬编码 1024）
  EMBEDDING_NORMALIZE   是否归一化（默认 true）
  EMBEDDING_TIMEOUT     超时秒数（默认 30）

Reranker（可选，缺省时 Hybrid 走 Weighted RRF fallback）：
  RERANKER_BASE_URL / RERANKER_API_KEY / RERANKER_MODEL

凭据只在进程内构造 provider 时使用，绝不进日志 / Snapshot / State / 报告。
"""

from __future__ import annotations

import os
from typing import Any

from app.context_engine.adapters.mapper import ModelCapability
from app.context_engine.providers.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
)
from app.context_engine.providers.factory import get_provider_factory
from app.context_engine.providers.runtime_env import get_context_engine_runtime_env


def embedding_capability_from_env(env: dict[str, str] | None = None) -> ModelCapability | None:
    """从环境变量构造 Embedding ModelCapability；缺配置返回 None（dense disabled）。"""
    env = env if env is not None else get_context_engine_runtime_env()
    base_url = env.get("EMBEDDING_BASE_URL", "").strip()
    model = env.get("EMBEDDING_MODEL", "").strip()
    api_key = env.get("EMBEDDING_API_KEY", "").strip()
    if not base_url or not model:
        return None
    try:
        dimension = int(env.get("EMBEDDING_DIMENSION", "0").strip())
    except ValueError:
        dimension = 0
    return ModelCapability(
        public_id="env_embedding",
        provider="openai-compatible",
        api_base_url=base_url,
        model_name=model,
        capability_type="embedding",
        embedding_dimension=dimension if dimension > 0 else None,
        normalize_embeddings=env.get("EMBEDDING_NORMALIZE", "true").strip().lower() in ("1", "true", "yes"),
        timeout_seconds=int(env.get("EMBEDDING_TIMEOUT", "30").strip() or 30),
        api_key_masked="****",
    )


def build_env_embedding_provider(
    env: dict[str, str] | None = None,
) -> tuple[Any, ModelCapability, str] | None:
    """从 .env 构造 (provider, capability, api_key)；缺配置返回 None。

    返回三元组供 RetrievalExecutor 注入；api_key 仅进程内持有。
    """
    env = env if env is not None else get_context_engine_runtime_env()
    capability = embedding_capability_from_env(env)
    if capability is None:
        return None
    api_key = env.get("EMBEDDING_API_KEY", "").strip()
    provider = OpenAICompatibleEmbeddingProvider(capability, api_key)
    return provider, capability, api_key


def reranker_capability_from_env(
    env: dict[str, str] | None = None,
) -> ModelCapability | None:
    """Build a non-secret Reranker capability from the process environment."""
    env = env if env is not None else get_context_engine_runtime_env()
    base_url = env.get("RERANKER_BASE_URL", "").strip()
    model = env.get("RERANKER_MODEL", "").strip()
    if not base_url or not model:
        return None
    try:
        timeout_seconds = int(env.get("RERANKER_TIMEOUT", "30").strip() or 30)
    except ValueError:
        timeout_seconds = 30
    provider = env.get("RERANKER_PROVIDER", "").strip()
    if not provider:
        provider = (
            "alibaba-model-studio"
            if "maas.aliyuncs.com" in base_url
            else "jina-cohere-compatible"
        )
    return ModelCapability(
        public_id="env_reranker",
        provider=provider,
        api_base_url=base_url,
        model_name=model,
        capability_type="reranker",
        timeout_seconds=timeout_seconds,
        api_key_masked="****",
    )


def build_env_reranker_provider(
    env: dict[str, str] | None = None,
) -> tuple[Any, ModelCapability, str] | None:
    """Build a process-local Reranker provider without exposing its key."""
    env = env if env is not None else get_context_engine_runtime_env()
    capability = reranker_capability_from_env(env)
    if capability is None:
        return None
    api_key = env.get("RERANKER_API_KEY", "").strip()
    provider = get_provider_factory().get_reranker_provider(capability, api_key)
    return provider, capability, api_key
# auto-appended module-level note: env-based provider 适配,主要在 LLMNotConfigured 时的 fallback。
