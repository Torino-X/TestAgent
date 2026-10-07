"""Context Engine 安全错误层级（CE-01-07 / ADR-CE-001 v2）。

- ``ContextEngineError``：Pydantic v2 FrozenModel 错误 DTO。immutable、
  ``extra="forbid"``、支持 ``model_dump(mode="json")`` 与安全 ``to_state_dict``。
  不写进 State 全文（设计文档 06：只保存轻量字段）。
- ``ContextEngineFailure``：进程内异常，持有 ``.error: ContextEngineError``。
  ``__str__``/``__repr__`` 只输出 code/stage，不输出 detail。
- ``raise_engine_error``：创建 ContextEngineError → 包装 ContextEngineFailure → 统一抛出。
- ``to_app_error``：接受 ContextEngineError / ContextEngineFailure / 未知 Exception，
  输出安全 AppError 参数；未知异常不回显原始字符串。

堆栈 / Provider Body / Prompt / API Key / SQL / storage path 永不进入
API / State / Snapshot。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.context_engine.models.value_objects import canonical_json

# 允许的 JSON 元数据值（设计文档 §35.1 JsonValue）
JsonValue = str | int | float | bool | None


class ContextEngineStage(str, Enum):
    """错误发生的流水线阶段（设计文档 §35.1 stage）。"""

    SCOPE = "scope"
    PROFILE = "profile"
    PLANNING = "planning"
    SOURCE = "source"
    RETRIEVAL = "retrieval"
    RERANK = "rerank"
    SELECTION = "selection"
    PREFLIGHT = "preflight"
    COMPRESSION = "compression"
    COMPOSE = "compose"
    SNAPSHOT = "snapshot"
    PAYLOAD = "payload"
    INDEX = "index"
    MEMORY = "memory"


class ContextEngineError(BaseModel):
    """Context Engine 领域错误 DTO（Pydantic v2 FrozenModel）。

    - ``frozen=True``：不可变；
    - ``extra="forbid"``：拒绝未知字段；
    - ``code``：稳定字符串（如 ``context.profile.not_found``）；
    - ``safe_metadata``：仅 JSON 可序列化值。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    detail: str = Field(min_length=1, max_length=1000)
    stage: ContextEngineStage
    retryable: bool = False
    recoverable: bool = True
    source_type: str | None = None
    safe_metadata: dict[str, JsonValue] = Field(default_factory=dict)

    def to_state_dict(self) -> dict[str, str | bool]:
        """写入 State 的轻量字段（不含 detail / safe_metadata 内部细节）。"""
        return {
            "code": self.code,
            "stage": self.stage.value,
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:
        # 不含 detail，防泄露
        return (
            f"ContextEngineError(code={self.code!r}, stage={self.stage.value!r}, "
            f"retryable={self.retryable})"
        )


class ContextEngineFailure(Exception):
    """进程内异常：持有 ``.error: ContextEngineError``。

    ``str(exc)`` / ``repr(exc)`` 只输出 code + stage，**不输出 detail**，
    防止 detail 中可能携带的敏感信息进入日志 / API / State。
    安全细节通过 ``exc.error.detail`` 显式读取。
    """

    def __init__(self, error: ContextEngineError) -> None:
        self.error = error
        super().__init__(error.detail)

    def __str__(self) -> str:
        return (
            f"ContextEngineFailure(code={self.error.code}, "
            f"stage={self.error.stage.value})"
        )

    def __repr__(self) -> str:
        return self.__str__()


def raise_engine_error(**kwargs) -> None:
    """创建 ContextEngineError → 包装 ContextEngineFailure → 统一抛出。

    所有 Context Engine 内部错误都应通过本入口抛出。
    捕获方用 ``except ContextEngineFailure as exc`` 获取安全负载。
    """
    raise ContextEngineFailure(ContextEngineError(**kwargs)) from None


# ── 与 AppError（统一 Envelope）的映射 ──────────────────────────────────
# 错误码段分配（沿用 core/exceptions.py 现有段位）：
#   51001 — 51099 : Context Engine 领域错误
#   51101 — 51199 : Context Engine 检索/记忆

_CONTEXT_ERROR_CODE = 51000
_RETRIEVAL_ERROR_CODE = 51100

# 未知异常的默认映射：安全 fallback，不回显内部细节
_UNKNOWN_FALLBACK = (51001, "Context Engine 内部错误")

# 检索/记忆相关 stage
_RETRIEVAL_STAGES = {
    ContextEngineStage.RETRIEVAL,
    ContextEngineStage.RERANK,
    ContextEngineStage.INDEX,
    ContextEngineStage.MEMORY,
}


def to_app_error(
    error: ContextEngineError | ContextEngineFailure | Exception,
) -> "tuple[int, str, dict[str, Any]]":
    """映射为 ``(code, message, detail)`` 三元组，供 AppError 构造使用。

    - 接受 ContextEngineError / ContextEngineFailure / 未知 Exception；
    - 未知异常不回显原始字符串（fallback 51001）。
    """
    if isinstance(error, ContextEngineFailure):
        error = error.error
    if isinstance(error, ContextEngineError):
        if error.stage in _RETRIEVAL_STAGES:
            code = _RETRIEVAL_ERROR_CODE + 1
        else:
            code = _CONTEXT_ERROR_CODE + 1
        detail: dict[str, Any] = {
            "context_code": error.code,
            "stage": error.stage.value,
            "retryable": error.retryable,
        }
        return code, error.detail, detail

    code, message = _UNKNOWN_FALLBACK
    return code, message, {}


def canonical_error_digest(error: ContextEngineError) -> str:
    """错误稳定摘要（用于幂等 / 审计去重），不含任何 secret。"""
    return canonical_json(
        {
            "code": error.code,
            "stage": error.stage.value,
            "retryable": error.retryable,
        }
    )
# auto-appended module-level note: context_engine 异常类型定义。
