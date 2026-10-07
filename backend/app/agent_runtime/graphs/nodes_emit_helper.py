"""Phase 2.8D — 统一节点 emit helper。

Phase 2.8D ADR-2.8D-4 / ADR-2.8D-5:把 build_for_tool_result / build_for_retry 调用下沉到 helper,
所有 v2 节点统一过 _emit_with_public_update,公开执行说明 payload 嵌入既有 SSE 事件,
不发新事件(向后兼容 2.5/2.6 chunk 流协议)。

设计契约:
* 仅替换 ctx.event_sink.emit 调用点,**不改节点内部逻辑**
* helper 内部按 event_type 选 builder:TOOL_FINISHED/TOOL_FAILED → build_for_tool_result,
  RETRYING → build_for_retry,其他 → 直传(向后兼容)
* split_into_chunks 切多帧后逐帧 emit(同 2.5/2.6 chunk 流协议)
* 不破坏 23 节点 emit 协议(Keyword-only args 严格匹配 RuntimeContext.AgentEventSink.emit)
* 不引入新的 SSE 事件名(2.5/2.6 沿用)
"""

from __future__ import annotations

from typing import Any

from app.agent.enums import AgentEventType
from app.agent.public_execution_update import PublicExecutionUpdate
from app.agent.public_execution_update_builder import (
    build_for_retry,
    build_for_tool_result,
    split_into_chunks,
)


async def _emit_with_public_update(
    *,
    task_id: str,
    node_name: str,
    event_type: str,
    payload: dict | None,
    ctx: Any,
    tool_name: str | None = None,
    success: bool | None = None,
    error: Any = None,
    elapsed_ms: int = 0,
    retry_strategy: str | None = None,
    retry_attempt: int = 1,
    retry_last_error: str = "",
    retry_next_attempt_in_seconds: float | None = None,
    **extra_kwargs: Any,
) -> list[dict]:
    """2.8D 统一 emit helper。

    与 ctx.event_sink.emit 签名一致(除 task_id 显式入参外),
    自动嵌入 public_execution_update 字段(若适用)。

    Args:
        task_id: 任务 public_id。
        node_name: 当前 LangGraph 节点名。
        event_type: AgentEventType 枚举值(字符串形式)。
        payload: 节点要 emit 的 payload 字典。
        ctx: RuntimeContext(取 settings_service / event_sink / graph_run_id)。
        tool_name: 工具名(给 build_for_tool_result / build_for_retry 用;默认 = node_name)。
        success: TOOL_FINISHED=True / TOOL_FAILED=False;其他事件忽略。
        error: 失败时的 error dict(TOOL_FAILED 用)。
        elapsed_ms: 工具执行耗时(TOOL_FINISHED/TOOL_FAILED 用)。
        retry_strategy: RETRYING 用;e.g. schema_feedback/degrade/backoff/same_inputs。
        retry_attempt: 当前第 N 次重试。
        retry_last_error: 上次失败原因文本。
        retry_next_attempt_in_seconds: 下次重试间隔(秒)。
        **extra_kwargs: 透传给 event_sink.emit 的其他 kwargs
            (event_id/sequence_no/idempotency_key 等)。

    Returns:
        emit 的结果列表(逐 chunk 一次 emit 一个)。
    """
    base_payload = dict(payload or {})
    effective_tool_name = tool_name or node_name

    # Phase 2.8D ADR-2.8D-5:不破坏现有 SSE 事件名与关键 payload;
    # public_execution_update 字段嵌进 payload,不新增事件。
    chunks: list[PublicExecutionUpdate] = []
    if event_type in {AgentEventType.TOOL_FINISHED.value, AgentEventType.TOOL_FAILED.value}:
        is_success = bool(success) if success is not None else (
            event_type == AgentEventType.TOOL_FINISHED.value
        )
        builder = build_for_tool_result(
            tool_name=effective_tool_name,
            success=is_success,
            data=base_payload.get("data") if isinstance(base_payload.get("data"), dict) else None,
            error=error,
        )
        chunks = split_into_chunks(builder)
    elif event_type == AgentEventType.RETRYING.value:
        builder = build_for_retry(
            tool_name=effective_tool_name,
            strategy=retry_strategy or "backoff",
            attempt=retry_attempt,
            last_error=retry_last_error,
            next_attempt_in_seconds=retry_next_attempt_in_seconds,
        )
        chunks = split_into_chunks(builder)
    # 其他事件类型不发 public_execution_update(向后兼容)

    emitted: list[dict] = []
    if not chunks:
        # 直传路径(向后兼容):只发一次 emit
        result = await ctx.event_sink.emit(
            task_id=task_id,
            graph_run_id=getattr(ctx, "graph_run_id", None) or "",
            node_name=node_name,
            event_type=event_type,
            title=base_payload.get("title", ""),
            content=base_payload.get("content", ""),
            payload=base_payload,
            **extra_kwargs,
        )
        emitted.append(result)
        return emitted

    # 多帧路径:每个 chunk 一次 emit,带 chunk_index/total 索引
    total = len(chunks)
    for idx, chunk in enumerate(chunks):
        merged = dict(base_payload)
        merged["public_execution_update"] = chunk
        if total > 1:
            merged["public_execution_update_chunk_index"] = idx
            merged["public_execution_update_chunk_total"] = total
        result = await ctx.event_sink.emit(
            task_id=task_id,
            graph_run_id=getattr(ctx, "graph_run_id", None) or "",
            node_name=node_name,
            event_type=event_type,
            title=base_payload.get("title", chunk.headline if hasattr(chunk, "headline") else ""),
            content=base_payload.get("content", chunk.summary if hasattr(chunk, "summary") else ""),
            payload=merged,
            **extra_kwargs,
        )
        emitted.append(result)
    return emitted


__all__ = ["_emit_with_public_update"]

# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (Phase 2.8D 节点 emit 统一化 helper):
#
#   v2 / v2_frozen 节点统一通过 ``_emit_with_public_update`` 发 SSE,而不是
#   自己直接 ``ctx.event_sink.emit(...)``。
#
#   这一下沉让以下两点一次解决:
#     - 不再每个节点重复写 build_for_tool_result / build_for_retry;
#     - split_into_chunks 切多帧(2.5 / 2.6 chunk 流协议)自动触发。
#
#   ⚠️ 注意:v3 节点(agent_runtime/graphs/test_plan/versions/v3/)用了
#   TestAgentToolAdapter 取代本 helper —— v3 路径上本文件基本不活跃,
#   v3 节点们走 adapter.emit(...) 而不是 _emit_with_public_update(...)
#
# 读代码时:
#   - 想搞懂"为什么节点 function 里没看到 emit" → 看 test_plan/versions/v3/ 对应
#     节点,它们走的是 adapter;这个 helper 是 v2 时代的 emit 抽象。
# ════════════════════════════════════════════════════════════════════════════════
