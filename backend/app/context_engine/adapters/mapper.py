"""ModelConfig ↔ 领域模型 Mapper。

ORM 与 Domain 分离：Mapper 负责把 ``app.models.config.ModelConfig``
(ORM) 转成 Context Engine 领域模型，不依赖 FastAPI / LangGraph。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelCapability:
    """模型能力元数据（Budget Manager / Provider 消费，来自 model_configs）。"""

    public_id: str
    provider: str
    api_base_url: str
    model_name: str
    capability_type: str = "chat"
    context_window_tokens: int | None = None
    default_max_output_tokens: int | None = None
    tokenizer_name: str | None = None
    provider_overhead_tokens: int | None = None
    embedding_dimension: int | None = None
    normalize_embeddings: bool | None = None
    rerank_instruction: str | None = None
    pre_rerank_limit: int | None = None
    score_type: str | None = None
    timeout_seconds: int = 120
    enable_thinking: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    # 明文 Key 永不进入 Mapper 产物；仅保留脱敏后的可辨标识。
    api_key_masked: str = "****"
    extra: dict = field(default_factory=dict)

    @property
    def effective_context_window(self) -> int | None:
        """可用窗口：优先取配置的 context_window_tokens。"""
        return self.context_window_tokens


def map_model_config_to_capability(cfg, mask_api_key_fn=None) -> ModelCapability:
    """把 ORM ``ModelConfig`` 映射为 ``ModelCapability``。

    ``mask_api_key_fn`` 可注入 ``app.core.crypto.mask_api_key`` 以生成掩码；
    为保持 Mapper 纯净（不依赖 crypto），默认只放 ``"****"``。
    """
    if cfg is None:
        raise ValueError("cfg is None; cannot map ModelConfig")
    masked = "****"
    if mask_api_key_fn is not None:
        try:
            masked = mask_api_key_fn(cfg.api_key_encrypted)
        except Exception:  # noqa: BLE001
            masked = "****"
    return ModelCapability(
        public_id=cfg.public_id,
        provider=cfg.provider,
        api_base_url=cfg.api_base_url,
        model_name=cfg.model_name,
        capability_type=getattr(cfg, "capability_type", None) or "chat",
        context_window_tokens=getattr(cfg, "context_window_tokens", None),
        default_max_output_tokens=getattr(cfg, "default_max_output_tokens", None),
        tokenizer_name=getattr(cfg, "tokenizer_name", None),
        provider_overhead_tokens=getattr(cfg, "provider_overhead_tokens", None),
        embedding_dimension=getattr(cfg, "embedding_dimension", None),
        normalize_embeddings=getattr(cfg, "normalize_embeddings", None),
        rerank_instruction=getattr(cfg, "rerank_instruction", None),
        pre_rerank_limit=getattr(cfg, "pre_rerank_limit", None),
        score_type=getattr(cfg, "score_type", None),
        timeout_seconds=int(getattr(cfg, "timeout_seconds", None) or 120),
        enable_thinking=bool(getattr(cfg, "enable_thinking", False)),
        temperature=getattr(cfg, "temperature", None),
        max_tokens=getattr(cfg, "max_tokens", None),
        api_key_masked=masked,
    )
# auto-appended module-level note: mapper: domain ↔ ContextEngine 双向字段映射。
