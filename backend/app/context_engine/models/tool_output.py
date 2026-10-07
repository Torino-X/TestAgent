"""Tool Output 治理模型：ManagedToolOutput / ToolOutputTruncation。

CE-02 WP-6：阈值由 ToolOutputPolicy 配置；small→inline、medium→truncate、
large/huge→payload 外置、binary→metadata-only + ref。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.context_engine.models.value_objects import Digest


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolOutputTruncation(FrozenModel):
    """截断元数据。"""

    mode: Literal["inline", "head_tail", "payload_ref", "metadata_only"]
    included_char_count: int = Field(ge=0)
    omitted_char_count: int = Field(ge=0)
    head_chars: int = Field(default=0, ge=0)
    tail_chars: int = Field(default=0, ge=0)


class ToolOutputPolicy(FrozenModel):
    """ToolOutputManager 策略配置。

    - ``inline_char_limit``：超过则截断 / 外置；
    - ``head_chars`` / ``tail_chars``：head/tail 截断长度；
    - ``store_full_output``：是否全文存储（默认 False）；
    - ``allow_llm_summary``：CE-02 固定 False（无 summarization placeholder）。
    """

    inline_char_limit: int = Field(default=4000, ge=0)
    head_chars: int = Field(default=1500, ge=0)
    tail_chars: int = Field(default=800, ge=0)
    store_full_output: bool = False
    allow_llm_summary: bool = False
    media_type_inline_limit: int = Field(default=200, ge=0)
    policy_key: str = "default"
    policy_version: str = "v1"


class ManagedToolOutput(FrozenModel):
    """治理后的 Tool Output（可落库到 tool_calls 扩展列）。"""

    tool_call_public_id: str
    user_id: int = Field(gt=0)
    preview: str = Field(max_length=20000)
    prompt_text: str | None = None
    output_char_count: int = Field(ge=0)
    output_size_bytes: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    output_sha256: Digest
    truncated: bool = False
    payload_ref: str | None = None
    policy: ToolOutputPolicy
    truncation_metadata: ToolOutputTruncation | None = None
    media_type: str | None = None

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "tool_call_public_id": self.tool_call_public_id,
            "output_char_count": self.output_char_count,
            "output_size_bytes": self.output_size_bytes,
            "estimated_tokens": self.estimated_tokens,
            "output_sha256": str(self.output_sha256),
            "truncated": self.truncated,
            "payload_ref": self.payload_ref,
            "policy_key": self.policy.policy_key,
        }
# auto-appended module-level note: tool_output 模型: ToolOutput 数据契约(产出 + 缓存键 + token)。
