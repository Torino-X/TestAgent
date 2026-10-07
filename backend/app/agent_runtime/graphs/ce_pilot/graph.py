"""LangGraph Pilot — 独立可编译可运行图（CE-02 WP-10）。

用户已确认：**不改 v3 图**。新建独立 Pilot 图，节点走 RuntimeContext +
ContextAwareLLMInvoker：
- ``init_node``：校验输入/初始化 state，不调 LLM；
- ``pilot_invoke_node``：经 ``ctx.context_llm_invoker.invoke(...)``，
  call_site=``ce.pilot.summarize``，写 ``context_state`` + 业务字段。

thread_id 必须 = task_public_id（复用 require_graph_thread_id，非 task_id）。
GraphRegistry 注册受 Feature Flag 控制，不改变 default graph。
"""

from __future__ import annotations

from typing import Any, Dict

from langgraph.graph import END, START, StateGraph

from app.agent_runtime.graphs.ce_pilot.state import CePilotState
from app.agent_runtime.graph_thread_id import require_graph_thread_id

GRAPH_NAME_PILOT = "ce_pilot"
GRAPH_VERSION_PILOT = "v1"
STATE_SCHEMA_VERSION_PILOT = 1

# 节点名
NODE_INIT = "ce_pilot_init"
NODE_INVOKE = "ce_pilot_invoke"
NODE_FAIL = "ce_pilot_fail"

# call_site（对应正式 ContextProfile ce.pilot.summarize.v1）
PILOT_CALL_SITE = "ce.pilot.summarize"

_RUNTIME_CONTEXT_KEY = "runtime_context"


def get_pilot_ctx(config: Dict[str, Any] | None) -> Any:
    if not config:
        return None
    configurable = config.get("configurable") or {}
    return configurable.get(_RUNTIME_CONTEXT_KEY)


async def init_node(state: CePilotState, *, ctx) -> dict:
    """校验输入 / 初始化 state，不调 LLM。"""
    if not state.get("task_public_id") and not state.get("task_id"):
        return {"last_error": {"code": "ce_pilot.no_task", "detail": "缺少 task 标识"}}
    if not state.get("user_id"):
        return {"last_error": {"code": "ce_pilot.no_user", "detail": "缺少 user_id"}}
    # 校验 thread_id = task_public_id（begin 前即校验，不调 LLM）
    thread_id = require_graph_thread_id(state, context="ce_pilot")
    if not thread_id:
        return {"last_error": {"code": "ce_pilot.thread_id_invalid", "detail": "thread_id 无效"}}
    return {
        "graph_name": GRAPH_NAME_PILOT,
        "graph_version": GRAPH_VERSION_PILOT,
        "state_schema_version": STATE_SCHEMA_VERSION_PILOT,
        "current_node": NODE_INIT,
    }


async def pilot_invoke_node(state: CePilotState, *, ctx) -> dict:
    """经 ctx.context_llm_invoker.invoke 执行 Pilot LLM 调用。"""
    invoker = getattr(ctx, "context_llm_invoker", None)
    if invoker is None:
        return {
            "last_error": {
                "code": "ce_pilot.invoker_missing",
                "detail": "RuntimeContext 未注入 context_llm_invoker",
            }
        }

    from app.context_engine.models.context import ContextRequest

    request = ContextRequest(
        user_id=str(state.get("user_id")),
        conversation_id=state.get("conversation_id"),
        task_id=state.get("task_public_id") or state.get("task_id"),
        call_site=PILOT_CALL_SITE,
        current_user_message=state.get("user_prompt") or "",
        thread_id=state.get("task_public_id"),
    )

    llm_task_profile = _pilot_llm_profile(state)

    try:
        result = await invoker.invoke(
            request=request,
            llm_task_profile=llm_task_profile,
            runtime_context=ctx,
        )
    except Exception as exc:  # noqa: BLE001 — 记安全错误
        return {
            "last_error": {
                "code": "ce_pilot.invoke_failed",
                "detail": str(exc)[:500],
            }
        }

    context_state = result.context_state_ref
    update: dict = {
        "snapshot_public_id": result.snapshot_public_id,
        "pilot_token_usage": result.token_usage,
        "current_node": NODE_INVOKE,
    }
    if context_state is not None:
        update["context_state"] = context_state.to_state_dict()
    value = getattr(result, "value", None)
    if isinstance(value, dict) and value.get("summary"):
        update["pilot_summary"] = value["summary"]
    elif isinstance(value, str):
        update["pilot_summary"] = value
    return update


async def fail_node(state: CePilotState, *, ctx) -> dict:
    return {"current_node": NODE_FAIL}


def _pilot_llm_profile(state: CePilotState) -> Any:
    """Pilot 的 LLMTaskProfile（正式注册，不硬编码临时）。"""
    from app.llm.task_profiles import LLMTaskProfile

    return LLMTaskProfile(
        name="CE_PILOT_SUMMARIZE",
        system_prompt=(
            "你是 TestAgent Context Engine Pilot。请基于提供的上下文输出 JSON："
            '{"summary": "<上下文要点摘要>"}'
        ),
        parser="json_strict",
        require_json=True,
        on_parse_failure="fallback_default",
        fallback_text='{"summary": ""}',
    )


def _bind_async(node_fn):
    """把 async def f(state, *, ctx) 包成 LangGraph 节点。"""

    async def _wrapped(state: CePilotState, config: Any) -> dict:
        ctx = get_pilot_ctx(config)
        if ctx is None:
            return {"last_error": {"code": "ce_pilot.no_ctx", "detail": "缺少 runtime_context"}}
        return await node_fn(state, ctx=ctx)

    _wrapped.__name__ = node_fn.__name__
    return _wrapped


def build_compiled_ce_pilot_graph(checkpointer: Any = None) -> Any:
    """构建并编译独立 Pilot 图。"""
    graph = StateGraph(CePilotState)

    graph.add_node(NODE_INIT, _bind_async(init_node))
    graph.add_node(NODE_INVOKE, _bind_async(pilot_invoke_node))
    graph.add_node(NODE_FAIL, _bind_async(fail_node))

    graph.add_edge(START, NODE_INIT)
    graph.add_conditional_edges(
        NODE_INIT,
        _route_init,
        {
            "invoke": NODE_INVOKE,
            "fail": NODE_FAIL,
        },
    )
    graph.add_conditional_edges(
        NODE_INVOKE,
        _route_invoke,
        {
            "end": END,
            "fail": NODE_FAIL,
        },
    )
    graph.add_edge(NODE_FAIL, END)

    return graph.compile(checkpointer=checkpointer)


def _route_init(state: CePilotState) -> str:
    if state.get("last_error"):
        return "fail"
    return "invoke"


def _route_invoke(state: CePilotState) -> str:
    if state.get("last_error"):
        return "fail"
    return "end"


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (LangGraph Pilot 图装配 CE-02 WP-10):
#
#   Phase 2.9A 衍生实验图(孤立可编译 + 可运行,不进生产默认):
#
#     节点序列:
#       init_node (sync, 校验输入/初始化 state, 不调 LLM)
#         → pilot_invoke_node (async, 调 ctx.context_llm_invoker.invoke(...))
#             call_site='ce.pilot.summarize'
#             业务字段: input_text / prompt_hint / context_state
#             输出: summary_text / trace / used_tokens
#           → finalize_node (写结果到 state.summary_published)
#
#   ⚠️ 不替代 test_plan 主图 ——
#     registry 注册受 graph_registry.feature_flags 控制,不修改 default graph。
#
# 关键约束(供开发者速查):
#   - RuntimeContext 走 _shared.runtime_context,所有节点都必须 await;
#   - ContextAwareLLMInvoker 是 call-site-aware 的 LLM 入口(CE-02 WP-08),
#     与 legacy LLMClient 不同,失败时返回 None 而不是 raise;
#   - 此图主要给 ContextEngine 的 Pilot/QA 测试用,代码层无流量;
#   - 修改时不要影响 test_plan/v3 已注册的图。
