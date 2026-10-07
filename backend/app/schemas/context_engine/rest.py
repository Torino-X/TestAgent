"""CE-05 Context REST Schema — 分页/错误/列表契约。

复用 app/core/response.ApiResponse 作为统一 envelope；此处补充分页与错误 DTO。
Cursor 为 opaque（AES-GCM 加密），API 侧不暴露 internal id。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class CursorPage(BaseModel, Generic[T]):
    """增长型列表响应：opaque cursor + 数据行。

    - ``items``：当前页数据
    - ``next_cursor``：下一页 opaque cursor（AES-GCM 加密 base64url），
      无更多页时为 None
    - ``total``：默认不计算，仅 include_total=true 的受控接口返回
    """

    items: list[T]
    next_cursor: str | None = None
    total: int | None = Field(default=None, description="仅 include_total=true 时返回")


class ErrorDetail(BaseModel):
    """统一错误体（data 内嵌）。"""

    code: str
    detail: str | None = None


class AuditItem(BaseModel):
    """审计/资源只读列表项的公共字段基底。"""

    public_id: str
    created_at: str | None = None


class PaginationParams(BaseModel):
    """分页查询参数契约（limit 默认 20，最大 100）。"""

    limit: int = Field(default=20, ge=1, le=100, description="每页条数，默认 20，最大 100")
    cursor: str | None = Field(default=None, description="上一页返回的 opaque cursor")
    include_total: bool = Field(default=False, description="是否计算 total（受控接口）")
# auto-appended module-level note: rest: ContextEngine REST response envelope(scope / source / payload)。
