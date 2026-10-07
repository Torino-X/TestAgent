"""CE-04 §四：工具层 MIG 路由共享 helper。

给 TestPlanGeneratorTool / TestPlanRegenTool 提供统一的 bridge 调用入口。

- MIG flag=false → 调用方走 legacy（LLMClient.generate）
- MIG flag=true  → 只走 ContextInvokerBridge；bridge 缺失视为明确错误
                   （返回 error 而非静默回退 legacy）。

工具经 ``context.context_llm_invoker``（_AgentContextProxy 透传）访问 bridge。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def invoke_via_bridge_or_none(
    *,
    context: Any,
    call_site: str,
    llm_task_profile: Any,
    current_goal: str,
    output_contract: str = "json",
    task_state_ref: dict | None = None,
    system_prompt: str | None = None,
) -> tuple[str, Any]:
    """经 bridge 生成。返回 (error_code_or_None, raw_text)。

    bridge 可用 → (None, raw_text)；bridge 缺失/失败 → (error_code, None)。
    绝不静默回退 legacy —— 由调用方根据返回值处理。
    """
    bridge = getattr(context, "context_llm_invoker", None)
    if bridge is None or not getattr(bridge, "available", False):
        return ("MIGRATION_INVOKER_UNAVAILABLE", None)

    try:
        bres = await bridge.generate(
            user_id=getattr(context, "user_internal_id", 0),
            call_site=call_site,
            llm_task_profile=llm_task_profile,
            current_goal=current_goal,
            task_state_ref=task_state_ref,
            output_contract=output_contract,
            system_prompt=system_prompt,
            user_content=current_goal,
            conversation_id=getattr(context, "conversation_internal_id", None),
            task_id=(
                getattr(context, "task_id", None)
                or getattr(context, "task_internal_id", None)
            ),
            runtime_context=context,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "MIGRATION_INVOKER_EXCEPTION | call_site=%s | error_type=%s | error=%s",
            call_site,
            type(exc).__name__,
            str(exc)[:300],
        )
        return (f"MIGRATION_INVOKER_EXCEPTION:{type(exc).__name__}", None)
    if bres is None or bres.value is None:
        return ("MIGRATION_INVOKER_FAILED", None)
    return (None, bres.value)


class _BridgeRawClient:
    """把 bridge 已取回的 raw text 包装为 LLMClient 形状（generate/continue_generation）。

    供工具在 MIG flag=true 时使用：LLM 调用已由 bridge 完成，本类只提供
    ``generate`` / ``continue_generation`` 接口让工具既有解析逻辑不改变。
    第二个参数（provider 重试请求）由工具解析逻辑自行处理。

    Bridge 返回的 ``value`` 是已经过 ``parse_result`` 处理的 Python 对象
    （通常是 dict）。工具下游 ``result_parser.is_json_truncated`` /
    ``parse_and_validate_json`` 仍然按字符串协议工作，所以这里必须把
    非字符串值重新序列化回 JSON 字符串，避免下游 ``.strip()`` AttributeError。
    """

    def __init__(self, raw_text: Any) -> None:
        if isinstance(raw_text, str):
            self._raw = raw_text
        else:
            self._raw = json.dumps(raw_text, ensure_ascii=False, indent=2)

    async def generate(self, *args, **kwargs) -> str:
        return self._raw

    async def continue_generation(self, *args, **kwargs) -> str:
        return ""


__all__ = ["invoke_via_bridge_or_none", "_BridgeRawClient"]
