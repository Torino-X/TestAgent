"""Retry / Provider 错误模型：SafeLLMError / LLMAttemptRef。

CE-02 WP-9：Provider Error Mapping。``SafeLLMError`` 只携带安全字段，
**不含 raw Provider 错误字符串 / 堆栈 / API key**。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SafeLLMError(FrozenModel):
    """Provider 错误的安全表示。

    - ``code``：稳定错误码（如 ``llm.provider.timeout``）；
    - ``detail``：脱敏描述（不含原始异常正文 / 请求体）；
    - ``provider``：Provider 名（可空，不含 endpoint / key）；
    - ``provider_request_id``：可空；
    - ``retryable``：网络 / 限流 → True；权限 / 400 → False；
    - ``context_length_error``：context_length 超限标记。
    """

    code: str = Field(min_length=1, max_length=64)
    detail: str = Field(min_length=1, max_length=1000)
    provider: str | None = Field(default=None, max_length=64)
    provider_request_id: str | None = Field(default=None, max_length=256)
    retryable: bool = False
    context_length_error: bool = False

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "provider": self.provider,
            "retryable": self.retryable,
            "context_length_error": self.context_length_error,
        }


class LLMAttemptRef(FrozenModel):
    """单次 LLM 尝试的安全引用（用于 attempts 累积）。"""

    attempt: int = Field(ge=1)
    snapshot_public_id: str
    provider_request_id: str | None = None
    error_code: str | None = None
    retryable: bool = False
    started_at: datetime
    finished_at: datetime | None = None
    kind: Literal["initial", "schema_retry", "context_length_retry", "provider_retry"] = "initial"
# auto-appended module-level note: retry 模型: RetryContext / RetryDecision, 与 _shared.retry_policy 字段兼容。
