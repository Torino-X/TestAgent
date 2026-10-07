"""RetryPolicy — Invoker 重试策略。

CE-02 整改一：Invoker 迁移到 Agent Runtime 层（app/agent_runtime/context/）。
Provider/Schema/ContextLength 重试次数由 RetryPolicy 控制。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """重试策略。"""

    max_provider_retries: int = 1
    max_schema_retries: int = 1
    max_context_length_retries: int = 1
