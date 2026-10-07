"""Unified API response structure."""

from __future__ import annotations

import uuid
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """Standard API response envelope.

    code 0 = success, non-zero = error.
    """

    code: int = Field(default=0, description="0 = success, non-zero = error code")
    message: str = Field(default="success")
    data: T | None = Field(default=None)
    request_id: str = Field(default_factory=lambda: f"req_{uuid.uuid4().hex[:12]}")


def success(data: Any = None, message: str = "success") -> dict:
    """Build a success response dict."""
    return ApiResponse(code=0, message=message, data=data).model_dump()


def error(code: int, message: str, data: Any = None) -> dict:
    """Build an error response dict."""
    return ApiResponse(code=code, message=message, data=data).model_dump()
# response:统一 API 响应壳子(ApiResponse[T] / success / error / fail);code 0 = success,非 0 = error,request_id 自动注入。
