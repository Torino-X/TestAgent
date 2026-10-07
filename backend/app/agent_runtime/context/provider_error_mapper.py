"""Provider Error Mapper — LLMProfileResult → SafeLLMError + usage/request_id 提取。

CE-02 整改一：Invoker 迁移到 Agent Runtime 层（app/agent_runtime/context/）。
Provider/Parser 错误映射属于 Application 层职责。
"""

from __future__ import annotations

from app.context_engine.models.retry_models import SafeLLMError


def map_provider_error(result) -> SafeLLMError:
    """把 LLMProfileResult 错误映射为 SafeLLMError。"""
    error_type = getattr(result, "error_type", None) or "llm_error"
    error_message = getattr(result, "error_message", None) or ""
    lower = (error_type + " " + error_message).lower()

    if "context" in lower and ("length" in lower or "token" in lower or "max" in lower):
        return SafeLLMError(
            code="llm.provider.context_length_error",
            detail="请求上下文超限",
            retryable=True,
            context_length_error=True,
        )
    if any(k in lower for k in ("timeout", "connection", "network", "rate", "429", "5")):
        return SafeLLMError(
            code="llm.provider.timeout" if "timeout" in lower else "llm.provider.transient",
            detail="Provider 暂时不可用",
            retryable=True,
        )
    if any(k in lower for k in ("403", "401", "permission", "auth", "400")):
        return SafeLLMError(
            code="llm.provider.permission",
            detail="Provider 拒绝请求",
            retryable=False,
        )
    return SafeLLMError(
        code="llm.provider.unknown",
        detail="Provider 调用失败",
        retryable=False,
    )


def extract_usage(result) -> dict[str, int] | None:
    """从 LLMProfileResult 提取 usage（不可用则 None，不造假）。"""
    usage = getattr(result, "usage", None) or getattr(result, "token_usage", None)
    if not usage:
        return None
    input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
    output_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    return {
        "input": int(input_tokens) if input_tokens is not None else None,
        "output": int(output_tokens) if output_tokens is not None else None,
    }


def provider_request_id(result) -> str | None:
    """提取 provider_request_id（仅存在于 Result/Attempt，不写 latency_json）。"""
    return getattr(result, "provider_request_id", None) or None


def llm_latency_ms(result) -> int | None:
    return getattr(result, "latency_ms", None) or None


def parse_result(result, parser_factory):
    """解析 result.parsed；parser_factory 可注入自定义解析。"""
    parsed = getattr(result, "parsed", None)
    if parsed is not None:
        return parsed
    if parser_factory is not None:
        return parser_factory(result)
    raise ValueError("LLM 响应无法解析（schema validation failure）")
