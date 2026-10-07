"""Settings schemas."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class ModelSettingsRequest(BaseModel):
    api_base_url: str
    api_key: Optional[str] = Field(default=None, description="If omitted, keep existing key")
    model_name: str
    timeout_seconds: int = 120
    enable_thinking: bool = Field(default=False)
    supports_vision: bool = Field(default=False)
    # CE-01: 模型能力类型（chat / reasoning / embedding / reranker /
    # compression / memory_extraction）
    capability_type: str = Field(default="chat")
    # CE-01: 能力元数据（可选）
    context_window_tokens: Optional[int] = None
    context_window_k: Optional[int] = Field(
        default=None,
        ge=1,
        description="Context window size in K tokens; persisted as context_window_tokens.",
    )
    default_max_output_tokens: Optional[int] = None
    embedding_dimension: Optional[int] = None
    normalize_embeddings: Optional[bool] = None
    rerank_instruction: Optional[str] = None
    pre_rerank_limit: Optional[int] = None
    score_type: Optional[str] = None
    config_name: Optional[str] = None
    provider: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    enabled: Optional[bool] = True
    is_default: Optional[bool] = True


class CapabilityModelConfigRequest(BaseModel):
    """单能力模型配置（多能力管理 API 用）。"""

    capability_type: str
    config_name: Optional[str] = None
    provider: Optional[str] = None
    api_base_url: str
    api_key: Optional[str] = Field(default=None, description="If omitted, keep existing key")
    model_name: str
    timeout_seconds: int = 120
    enable_thinking: bool = False
    supports_vision: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    context_window_tokens: Optional[int] = None
    context_window_k: Optional[int] = Field(
        default=None,
        ge=1,
        description="Context window size in K tokens; persisted as context_window_tokens.",
    )
    default_max_output_tokens: Optional[int] = None
    embedding_dimension: Optional[int] = None
    normalize_embeddings: Optional[bool] = None
    rerank_instruction: Optional[str] = None
    pre_rerank_limit: Optional[int] = None
    score_type: Optional[str] = None
    enabled: bool = True
    is_default: bool = True


class ModelSettingsResponse(BaseModel):
    api_base_url: str
    api_key_masked: str
    model_name: str
    timeout_seconds: int
    context_window_k: Optional[int] = None


class ModelTestResponse(BaseModel):
    success: bool
    message: str
    latency_ms: float | None = None


class KnowledgeBaseSettingsRequest(BaseModel):
    api_base_url: str
    api_key: Optional[str] = Field(default=None)
    knowledge_base_id: Optional[str] = None
    default_top_k: int = 5
    timeout_seconds: int = 30


class KnowledgeBaseSettingsResponse(BaseModel):
    api_base_url: str
    api_key_masked: str
    knowledge_base_id: Optional[str] = None
    default_top_k: int
    timeout_seconds: int
    enabled: bool


class UploadSettingsResponse(BaseModel):
    max_file_size_mb: int
    max_files_per_conversation: int
    allowed_extensions: List[str]


class UploadSettingsRequest(BaseModel):
    max_file_size_mb: int
    max_files_per_conversation: int
    allowed_extensions: List[str]


# ── Phase 2.9C narrative settings ────────────────────────────────────────


class NarrativeSettingsRequest(BaseModel):
    """PATCH body for ``/api/settings/narrative``."""

    enabled: bool = Field(
        default=True,
        description="Whether tool-call cards show public narrative output.",
    )


class NarrativeSettingsResponse(BaseModel):
    enabled: bool
    detail_level: str
    detail_level_source: str = "env"
    detail_level_options: List[str] = Field(
        default_factory=lambda: ["concise", "standard", "detailed"],
    )
