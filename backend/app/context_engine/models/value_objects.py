"""值对象：PublicId / WorkspaceKey / Digest / TokenCount / 版本字符串。

只依赖 Python 标准库，不依赖 FastAPI / SQLAlchemy / LangGraph。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, ClassVar

from pydantic_core import core_schema

_PUBLIC_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


def _build_str_schema(validate_func) -> core_schema.CoreSchema:
    """构造 Pydantic 兼容的 str 子类 schema（带校验）。"""

    def _validate(value: Any) -> str:
        return validate_func(str(value))

    return core_schema.no_info_after_validator_function(
        _validate,
        core_schema.str_schema(),
    )


class PublicId(str):
    """安全的业务公开标识符（对外引用，不使用 DB BIGINT）。"""

    @classmethod
    def validate(cls, value: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("public_id 不能为空")
        if not _PUBLIC_ID_RE.match(value):
            raise ValueError(
                f"public_id 非法: {value!r} (须为 3-64 位小写字母/数字/下划线)"
            )
        return value

    def __new__(cls, value: str) -> "PublicId":
        return str.__new__(cls, cls.validate(value))

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler) -> core_schema.CoreSchema:
        return _build_str_schema(cls.validate)


class WorkspaceKey(str):
    """工作区键。禁止从文件名推断（由显式配置或数据库列提供）。

    WP-BE-09：允许 ``conversation:{public_id}`` 格式（``:`` 是 Conversation
    Project Space 的正式 scope key 分隔符，见 task_scope_validator /
    context_memory 的约定）。列宽 191（context_workspace_instructions 等）。
    """

    _RESERVED = {"", "system", "global", "default", "public", "shared"}

    @classmethod
    def validate(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("workspace_key 必须是字符串")
        if not re.match(r"^[a-zA-Z0-9_:.-]{1,191}$", value):
            raise ValueError(f"workspace_key 非法: {value!r}")
        if value.lower() in cls._RESERVED:
            raise ValueError(f"workspace_key 使用保留字: {value!r}")
        return value

    def __new__(cls, value: str | None) -> "WorkspaceKey | None":
        validated = cls.validate(value)
        if validated is None:
            return None  # type: ignore[return-value]
        return str.__new__(cls, validated)

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler) -> core_schema.CoreSchema:
        return _build_str_schema(cls.validate)


class Digest(str):
    """SHA-256 内容摘要（稳定、跨进程可复现）。"""

    @classmethod
    def of(cls, data: str | bytes) -> "Digest":
        raw = data.encode("utf-8") if isinstance(data, str) else data
        return cls(hashlib.sha256(raw).hexdigest())

    @classmethod
    def validate(cls, value: str) -> str:
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError("digest 必须是 64 位 hex")
        int(value, 16)  # 校验 hex
        return value

    def __new__(cls, value: str) -> "Digest":
        return str.__new__(cls, cls.validate(value))

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler) -> core_schema.CoreSchema:
        return _build_str_schema(cls.validate)


class TokenCount(int):
    """非负 token 数。"""

    def __new__(cls, value: int) -> "TokenCount":
        if value < 0:
            raise ValueError("token count 不能为负")
        return int.__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler) -> core_schema.CoreSchema:
        def _validate(value: Any) -> int:
            return TokenCount(int(value))

        return core_schema.no_info_after_validator_function(
            _validate,
            core_schema.int_schema(),
        )


class VersionString(str):
    """语义版本字符串，如 ``v1`` / ``v1.2.0``。"""

    @classmethod
    def validate(cls, value: str) -> str:
        if not isinstance(value, str) or not re.match(r"^v?\d+(\.\d+){0,2}$", value):
            raise ValueError(f"版本字符串非法: {value!r}")
        return value

    def __new__(cls, value: str) -> "VersionString":
        return str.__new__(cls, cls.validate(value))

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler) -> core_schema.CoreSchema:
        return _build_str_schema(cls.validate)


def canonical_json(value: Any) -> str:
    """稳定可复现的 JSON 序列化（用于 Digest / 幂等键）。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
# auto-appended module-level note: value_objects 模型: 跨模块 frozen dataclass (TokenCount / BudgetState 等)。
