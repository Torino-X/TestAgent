"""Public contracts for the optional Company RAG query provider.

TestAgent never manages the external knowledge base.  It stores a connection
configuration and sends read-only search requests only.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class CompanyRagConfigPayload(BaseModel):
    enabled: bool = True
    api_base_url: HttpUrl
    api_key: str | None = Field(default=None, max_length=4096)
    timeout_seconds: int = Field(default=30, ge=3, le=120)
    top_k: int = Field(default=5, ge=1, le=20)
    similarity_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    retrieve_strategy: int = Field(default=3, ge=1, le=3)


class CompanyRagConfigPublic(BaseModel):
    config_id: str | None = None
    enabled: bool = False
    api_base_url: str = ""
    api_key_masked: str = ""
    api_key_set: bool = False
    timeout_seconds: int = 30
    top_k: int = 5
    similarity_threshold: float = 0.35
    retrieve_strategy: int = 3
    last_test_status: str | None = None
    last_test_message: str | None = None
    updated_at: str | None = None


class CompanyRagConnectionTestResult(BaseModel):
    success: bool
    latency_ms: int
    status: str
    message: str
    error_code: str | None = None
    tested_at: str


class CompanyRagSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=20)
    similarity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    retrieve_strategy: int | None = Field(default=None, ge=1, le=3)


class CompanyRagHit(BaseModel):
    id: str = ""
    title: str = ""
    content: str
    score: float | None = None
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompanyRagSearchResult(BaseModel):
    hits: list[CompanyRagHit] = Field(default_factory=list)
    hit_count: int = 0
    elapsed_ms: int = 0
