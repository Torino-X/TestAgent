"""run_legacy_kb_fallback — 字节级复用 search_knowledge_node 主体 (Phase 2.3)。

禁令 #1 / 防 Risk #5: fallback 必须保留 Phase 2.1 字节级契约,
否则会破坏 7 个 kb_skip 测试 + 74 个 Phase 2.2 测试。

本函数镜像 nodes_pre_confirm.py:294-359 search_knowledge_node 主体;
Prep Agent 触发 fallback 时直接调用,确保对外事件序列完全一致。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.agent.enums import AgentEventType
from app.agent_runtime.adapters.test_agent_tool_adapter import (
    TestAgentToolAdapter,
    UnknownToolError as _UnknownToolError,
)

logger = logging.getLogger(__name__)


# ── node name 常量 (与 nodes_pre_confirm.py 保持一致) ──────────────────

NODE_SEARCH_KNOWLEDGE = "search_knowledge"


def _adapter(ctx) -> Optional[TestAgentToolAdapter]:
    """Mirror nodes_pre_confirm._adapter — 取 ctx.tool_adapter。"""
    return getattr(ctx, "tool_adapter", None)


def _completed_nodes(state: dict, name: str) -> list:
    completed = list(state.get("completed_nodes") or [])
    if name not in completed:
        completed.append(name)
    return completed


def _knowledge_search_inputs_from_state(state: dict) -> dict[str, Any]:
    retrieval_plan = state.get("retrieval_plan_snapshot")
    retrieval_plan = retrieval_plan if isinstance(retrieval_plan, dict) else {}
    query = str(
        retrieval_plan.get("query")
        or state.get("user_prompt")
        or state.get("goal")
        or state.get("dynamic_goal")
        or "生成测试方案"
    ).strip()
    inputs: dict[str, Any] = {"query": query}
    top_k = retrieval_plan.get("top_k")
    if top_k is not None:
        try:
            inputs["top_k"] = int(top_k)
        except (TypeError, ValueError):
            logger.warning("COMPANY_RAG_QUERY_INPUT_INVALID | field=top_k | value=%r", top_k)
    return inputs


def _degraded_knowledge_result(
    *,
    envelope: dict[str, Any] | None,
    query: str,
    reason: str,
) -> dict[str, Any]:
    error = (envelope or {}).get("error") or {}
    if not isinstance(error, dict):
        error = {"message": str(error)}
    return {
        "similar_projects": [],
        "standards": [],
        "terms": [],
        "hits": [],
        "query": query,
        "hit_count": 0,
        "confidence": "low",
        "elapsed_ms": 0,
        "direct_answer_eligible": False,
        "degraded": True,
        "source": "company_rag",
        "skip_reason": reason,
        "error_code": str(error.get("code") or reason),
        "error_message": str(error.get("message") or reason),
    }


async def run_legacy_kb_fallback(state: dict, *, ctx) -> dict:
    """字节级镜像 ``nodes_pre_confirm.search_knowledge_node``。

    Prep Agent 不可恢复时(预算耗尽 / 权限永久拒 / 工具异常 / fail 决策)调用,
    走 single-shot KB 路径,守卫 F018 7 个 kb_skip 测试 + Phase 2.2 测试。
    """
    completed = _completed_nodes(state, NODE_SEARCH_KNOWLEDGE)
    adapter = _adapter(ctx)
    skip_reason = state.get("kb_skip_reason")

    if skip_reason:
        # skip 分支: synthetic TOOL_FINISHED + KNOWLEDGE_SUMMARY
        await ctx.event_sink.emit(
            task_id=str(ctx.task_internal_id),
            graph_run_id=f"run-{ctx.task_internal_id}",
            node_name=NODE_SEARCH_KNOWLEDGE,
            event_type=AgentEventType.TOOL_FINISHED.value,
            title="KnowledgeSearchTool 已跳过",
            content="",
            payload={
                "tool_name": "KnowledgeSearchTool",
                "skipped": True,
                "skip_reason": skip_reason,
            },
        )
        await ctx.event_sink.emit(
            task_id=str(ctx.task_internal_id),
            graph_run_id=f"run-{ctx.task_internal_id}",
            node_name=NODE_SEARCH_KNOWLEDGE,
            event_type=AgentEventType.KNOWLEDGE_SUMMARY.value,
            title="知识库检索完成",
            content="",
            payload={"skipped": True, "reason": skip_reason},
        )
        return {
            "knowledge_search_result": {"skip_reason": skip_reason},
            "kb_skip_reason": skip_reason,
            "current_node": NODE_SEARCH_KNOWLEDGE,
            "completed_nodes": completed,
        }

    if adapter is None:
        return {
            "knowledge_search_result": _degraded_knowledge_result(
                envelope=None,
                query=str(_knowledge_search_inputs_from_state(state).get("query") or ""),
                reason="tool_adapter_missing",
            ),
            "current_node": NODE_SEARCH_KNOWLEDGE,
            "completed_nodes": completed,
        }

    kb_inputs = _knowledge_search_inputs_from_state(state)
    try:
        envelope = await adapter.execute(
            tool_name="KnowledgeSearchTool",
            inputs=kb_inputs,
            ctx_runtime=ctx,
        )
    except _UnknownToolError:
        # 测试/stub:静默跳过 (mirror nodes_pre_confirm)
        envelope = {
            "success": False,
            "data": None,
            "summary": "KnowledgeSearchTool unavailable.",
            "warnings": ["KnowledgeSearchTool is not registered."],
            "error": {"code": "KNOWLEDGE_TOOL_UNAVAILABLE", "message": "tool missing"},
        }

    kb_failed = not envelope.get("success", True)
    kb_result = (
        envelope.get("data")
        if isinstance(envelope.get("data"), dict) and not kb_failed
        else _degraded_knowledge_result(
            envelope=envelope,
            query=str(kb_inputs.get("query") or ""),
            reason="tool_failed" if kb_failed else "empty_result",
        )
    )
    logger.info(
        "COMPANY_RAG_FALLBACK_RESULT | node=legacy_kb_fallback | task_id=%s | success=%s | "
        "degraded=%s | hit_count=%s | error_code=%s",
        getattr(ctx, "task_internal_id", None),
        bool(envelope.get("success", True)),
        bool(kb_result.get("degraded")),
        kb_result.get("hit_count"),
        (envelope.get("error") or {}).get("code") if isinstance(envelope.get("error"), dict) else None,
    )

    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_SEARCH_KNOWLEDGE,
        event_type=AgentEventType.KNOWLEDGE_SUMMARY.value,
        title="知识库检索完成",
        content="",
        payload=kb_result,
    )

    return {
        "knowledge_search_result": kb_result,
        "current_node": NODE_SEARCH_KNOWLEDGE,
        "completed_nodes": completed,
    }


__all__ = ["run_legacy_kb_fallback", "NODE_SEARCH_KNOWLEDGE"]
