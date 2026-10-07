"""ModelCapabilityResolver — 模型能力解析。

从 ``ModelCapability``（由 Mapper 从 model_configs 产出）解析：
- context_window（未知时不得假设 200K）
- 是否有 embedding / reranker 能力
- tokenizer 是否可用
"""

from __future__ import annotations

from dataclasses import dataclass

from app.context_engine.adapters.mapper import ModelCapability
from app.context_engine.errors import (
    ContextEngineError,
    ContextEngineStage,
    raise_engine_error,
)

# 明确禁止：未知窗口时不得假设 200K
UNKNOWN_WINDOW = None

_SUPPORTED_CAPABILITIES = {
    "chat",
    "reasoning",
    "embedding",
    "reranker",
    "compression",
    "memory_extraction",
}


@dataclass(frozen=True)
class ResolvedCapability:
    """解析后的能力结果。"""

    capability_type: str
    context_window: int | None
    default_max_output_tokens: int | None
    tokenizer_available: bool
    embedding_dimension: int | None
    supports_rerank: bool
    known_model: bool


class ModelCapabilityResolver:
    """从 ModelCapability 解析能力。不发起任何 I/O（纯函数）。"""

    def __init__(self, supported_capabilities: set[str] | None = None) -> None:
        self._supported = supported_capabilities or _SUPPORTED_CAPABILITIES

    def resolve(self, capability: ModelCapability) -> ResolvedCapability:
        capability_type = capability.capability_type or "chat"
        if capability_type not in self._supported:
            raise_engine_error(
                code="context.model.unsupported_capability",
                detail=f"不支持的模型能力类型: {capability_type!r}",
                stage=ContextEngineStage.PROFILE,
                retryable=False,
                recoverable=True,
                safe_metadata={"capability_type": capability_type},
            )
        context_window = capability.effective_context_window
        if context_window is not None and context_window <= 0:
            raise_engine_error(
                code="context.model.invalid_window",
                detail="context_window_tokens 必须为正数",
                stage=ContextEngineStage.PROFILE,
                retryable=False,
                recoverable=True,
            )
        return ResolvedCapability(
            capability_type=capability_type,
            context_window=context_window,
            default_max_output_tokens=capability.default_max_output_tokens,
            tokenizer_available=bool(capability.tokenizer_name),
            embedding_dimension=capability.embedding_dimension,
            supports_rerank=capability_type == "reranker",
            known_model=context_window is not None,
        )

    @staticmethod
    def unknown_window_error() -> ContextEngineError:
        """窗口未知：Planner 必须提高 Safety Margin，不得假设 200K。"""
        return ContextEngineError(
            code="context.model.unknown_window",
            detail="模型上下文窗口未知，禁止假设 200K；已提高 Safety Margin",
            stage=ContextEngineStage.PLANNING,
            retryable=False,
            recoverable=True,
        )
# auto-appended module-level note: model 能力解析: 把 model name 映射到 max_tokens + context_window。
