"""F020 — Pydantic schemas for the image-understanding configuration API.

Mirrors ``KnowledgeConfigPayload`` and friends; frontend sees only
the masked API key.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class ImageUnderstandingConfigPayload(BaseModel):
    """Save / replace the per-user image-understanding configuration."""

    api_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="Vision LLM OpenAI-compatible base URL.",
    )
    api_key: Optional[str] = Field(
        default=None,
        description="If omitted, the existing encrypted key is kept.",
    )
    model_name: str = Field(default="qwen-vl-plus")
    timeout_seconds: int = Field(default=60, ge=5, le=300)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=32768)
    enable_in_doc_parsing: bool = Field(default=False)


class ImageUnderstandingConfigPublic(BaseModel):
    """API-facing config (no plaintext key)."""

    config_id: Optional[str] = None
    api_base_url: str
    api_key_masked: str = ""
    api_key_set: bool = False
    model_name: str
    timeout_seconds: int
    max_tokens: Optional[int] = None
    enable_in_doc_parsing: bool
    last_test_status: Optional[str] = None
    last_test_message: Optional[str] = None
    last_test_at: Optional[str] = None
    updated_at: Optional[str] = None


class ImageUnderstandingConfigSaveResult(BaseModel):
    config_id: Optional[str] = None
    saved: bool = True
    api_key_set: bool
    api_key_masked: str = ""
    updated_at: str


class ImageUnderstandingTestConnectionResult(BaseModel):
    success: bool
    latency_ms: int
    status: Literal["success", "failed"]
    message: str
    error_code: Optional[str] = None
    tested_at: str# schemas.image_understanding:F020 图文理解配置 Pydantic 契约;前端只见脱敏 API key,与 KnowledgeConfig 对称。
