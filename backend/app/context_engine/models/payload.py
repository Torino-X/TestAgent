"""Payload 模型：ContextPayloadRef / PayloadStoreCommand。

CE-02 WP-5：Payload 外置。``ContextPayloadRef`` 不含 storage_key / 路径
（安全：storage key/path 永不进入 API/State/Prompt/Log）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.context_engine.models.value_objects import Digest


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContextPayloadRef(FrozenModel):
    """对已存储 Payload 的引用（state-safe，无 storage_key/路径）。"""

    payload_public_id: str
    storage_backend: str
    owner_user_id: int = Field(gt=0)
    content_sha256: Digest | None = None
    size_bytes: int = Field(ge=0)
    media_type: str | None = None
    expires_at: datetime | None = None

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "payload_public_id": self.payload_public_id,
            "storage_backend": self.storage_backend,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }


class PayloadStoreCommand(FrozenModel):
    """put 命令。键由服务端生成（不接受客户端 key）。

    ``content`` 可为 str / bytes；``media_type`` 用于二进制判断。
    """

    user_id: int = Field(gt=0)
    content: str | bytes
    media_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None
    allow_overwrite: bool = False
# auto-appended module-level note: payload 模型: ContextPayload 数据契约(白名单, 不含 storage_key)。
