"""TestAgentToolAdapter — LangGraph 侧唯一调 ToolExecutor 的入口。

设计约束:
* 白名单硬校验(Rule 12):未在白名单的工具 → UnknownToolError
* 不带 AsyncSession / asyncio.Task(Rule 10):session 现场通过 ``ctx_runtime.session_factory()`` 取
* 工具信封字节级等价 Legacy:返回 dict 与 Legacy ToolExecutor.run 一致
* 副作用(SSE 事件 / tool_calls 行)与 Legacy orchestrator 完全对应
* 不在 Module 级 import 重型 Legacy 模块,避免 LangGraph 节点 import 链变长

Phase 2.1 范围内:节点函数通过本适配器调工具,不再直接 import ToolExecutor。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncContextManager, Awaitable, Callable, Dict, List, Optional, Protocol
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.public_execution_update_builder import (
    build_for_retry,
    build_for_tool_result,
    build_tool_display_input,
    build_tool_display_output,
    split_into_chunks,
)
from app.agent.retry_policy import RetryContext, RetryDecision, RetryPolicy

from app.agent_runtime.runtime_context import AgentEventSink, RuntimeContext

logger = logging.getLogger(__name__)


# ── 白名单(9-tool set,与 backend/app/tools/register.py:15-24 一致)──

DEFAULT_TOOL_WHITELIST: frozenset[str] = frozenset({
    "RequirementParserTool",
    "TemplateParserTool",
    "KnowledgeSearchTool",
    "SectionSuggestionTool",
    "TestPlanGeneratorTool",
    "TestPlanRegenTool",
    "ResultReviewTool",
    "WordExportTool",
    "DocxFormatCheckTool",
})

# 最小输入校验(Rule 13);与 Legacy orchestrator 期望一致
_REQUIRED_KEYS: Dict[str, List[str]] = {
    "RequirementParserTool": ["requirement_file_id"],
    "TemplateParserTool": ["template_file_id"],
    "TestPlanRegenTool": ["section_ids", "issues"],
}

_STATE_TRANSFER_FIELDS: tuple[str, ...] = (
    # pre-confirm fields
    "requirement_analysis",
    "template_structure",
    "knowledge_search_result",
    "user_prompt",
    "section_suggestions",
    "section_confirm_config",
    "template_file_id",
    # post-confirm / sub-agent fields
    "test_plan_content",
    "review_standard",
    "review_result",
    "artifact",
    "format_check_result",
    "pending_format_losses",
    "format_loss_confirmation",
)


# ── 异常 ─────────────────────────────────────────────────────────────────


class UnknownToolError(KeyError):
    """``tool_name`` 不在白名单内。"""


class ToolAdapterError(RuntimeError):
    """Adapter 内部意外失败。"""


# ── 协议:ToolExecutorLike ────────────────────────────────────────────────


class ToolExecutorLike(Protocol):
    """ToolExecutor 的最小合约;测试可传 stub。"""

    async def run(
        self,
        tool_name: str,
        inputs: Dict[str, Any],
        context: Any,
        retry_context: Optional[RetryContext] = None,
    ) -> Dict[str, Any]: ...


# ── AgentContext shim ─────────────────────────────────────────────────────


@dataclass
class _AgentContextProxy:
    """给工具用的最小 AgentContext 视图。

    现场构造,绝不逃出 ``execute()``。不带 AsyncSession —— 工具要 DB 时
    自取 session(本阶段测试中可直接走 ``ctx_runtime.session_factory()``)。
    """

    task_id: str
    conversation_id: str
    user_id: str
    user_internal_id: int
    task_internal_id: int
    conversation_internal_id: int
    settings_service: Any
    session_factory: Any = None
    session: Any = None
    context_llm_invoker: Any = None
    # Full request-scoped runtime.  CE-backed legacy tools need this for
    # session/model configuration and must not fabricate a partial context.
    runtime_context: Any = None
    llm_client: Any = None
    task_flag_resolver: Any = None
    tool_progress_emitter: Optional[Callable[..., Awaitable[None]]] = None
    requirement_file_id: Optional[str] = None
    template_file_id: Optional[str] = None
    user_prompt: str = ""

    # 工具可能读写的中间数据
    requirement_analysis: Optional[Dict[str, Any]] = None
    template_structure: Optional[Dict[str, Any]] = None
    knowledge_search_result: Optional[Dict[str, Any]] = None
    section_suggestions: Optional[Dict[str, Any]] = None
    section_confirm_config: Optional[Dict[str, Any]] = None
    test_plan_content: Optional[Dict[str, Any]] = None
    review_result: Optional[Dict[str, Any]] = None
    review_standard: Optional[Dict[str, Any]] = None
    artifact: Optional[Dict[str, Any]] = None
    format_check_result: Optional[Dict[str, Any]] = None
    pending_format_losses: Optional[List[Dict[str, Any]]] = None
    format_loss_confirmation: Optional[Dict[str, Any]] = None
    format_loss_timeout_seconds: int = 300

    async def emit_tool_progress(self, progress_message: str, **metadata: Any) -> None:
        """Emit user-facing runtime progress for the current tool call."""
        if self.tool_progress_emitter is None:
            return
        await self.tool_progress_emitter(progress_message, **metadata)


# ── Adapter ──────────────────────────────────────────────────────────────


class TestAgentToolAdapter:
    """9-tool 适配器:白名单 + 信封 + 副作用 + 速率。

    节点调用::

        result = await adapter.execute(
            tool_name="RequirementParserTool",
            inputs={"requirement_file_id": fid},
            ctx_runtime=ctx,
        )
        # result 与 Legacy ToolExecutor.run() 返回值字节级一致
    """

    def __init__(
        self,
        *,
        tool_executor: ToolExecutorLike,
        tool_whitelist: Optional[frozenset[str]] = None,
        retry_policy: Optional[RetryPolicy] = None,
        event_sink: AgentEventSink,
        session_factory: Callable[[], AsyncContextManager[AsyncSession]],
        clock: Callable[[], datetime],
        min_visible_seconds: float = 0.8,
        transition_seconds: float = 0.12,
        tool_call_recorder: Optional[
            Callable[[Dict[str, Any]], Awaitable[None]]
        ] = None,
    ) -> None:
        self._executor = tool_executor
        self._whitelist = tool_whitelist or DEFAULT_TOOL_WHITELIST
        self._retry = retry_policy or RetryPolicy(max_retries=3)
        self._sink = event_sink
        self._session_factory = session_factory
        self._clock = clock
        self._min_visible_seconds = float(min_visible_seconds)
        self._transition_seconds = float(transition_seconds)
        self._recorder = tool_call_recorder
        # Phase 2.9B.6: 最近一次工具执行的真实终态 event_id(LiveAgentEventSink
        # emit 返回的 event_id)。节点在 execute 返回后调用 ``last_terminal_event_id()``
        # 构造 PendingNarrative.source_event_id,不再写空串。
        self._last_terminal_event_id: Optional[str] = None

    # ── 公共 API ────────────────────────────────────────────────────────

    def last_terminal_event_id(self) -> Optional[str]:
        """返回最近一次工具执行的最终 terminal 事件 event_id(可能 None)。"""
        return self._last_terminal_event_id

    async def execute(
        self,
        *,
        tool_name: str,
        inputs: Dict[str, Any],
        ctx_runtime: RuntimeContext,
        attempt: int = 1,
        retry_context: Optional[RetryContext] = None,
        graph_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # ── Rule 12: 白名单
        if tool_name not in self._whitelist:
            raise UnknownToolError(tool_name)

        # ── Rule 13: 最小输入校验(不调 tool,直接返回 canonical error envelope)
        missing = [
            k for k in _REQUIRED_KEYS.get(tool_name, []) if not inputs.get(k)
        ]
        if missing:
            # Phase 2.9A.X 诊断：记录被拒绝的完整 inputs 以便排查
            logger.warning(
                "ToolAdapter: %s 缺少必填字段 | missing=%s | "
                "input_keys=%s | input_values_preview=%s",
                tool_name,
                missing,
                sorted(inputs.keys()) if isinstance(inputs, dict) else "?",
                {k: str(v)[:100] for k, v in (inputs.items() if isinstance(inputs, dict) else [])},
            )
            return self._missing_inputs_envelope(
                tool_name=tool_name, attempt=attempt, missing=missing,
                task_id=str(ctx_runtime.task_internal_id),
            )

        # ── 计时 + 执行
        started_at = self._clock()
        tool_call_id = f"{tool_name}-{uuid4().hex}"
        # Phase 2.9B.6: 每次执行重置终态 event_id,避免上一次调用的残留串被
        # 误当成本次的 source_event_id。
        self._last_terminal_event_id = None
        await self._emit_tool_started(
            tool_name=tool_name,
            inputs=inputs,
            attempt=attempt,
            tool_call_id=tool_call_id,
            ctx_runtime=ctx_runtime,
        )
        session: Any = None
        try:
            async with self._session_factory() as session:
                ctx_proxy = self._build_proxy(
                    tool_name=tool_name,
                    attempt=attempt,
                    tool_call_id=tool_call_id,
                    ctx_runtime=ctx_runtime,
                    inputs=inputs,
                    session=session,
                    graph_state=graph_state,
                )
                result = await self._executor.run(
                    tool_name, dict(inputs), ctx_proxy, retry_context,
                )
                commit = getattr(session, "commit", None)
                if callable(commit):
                    await commit()
        except Exception as exc:  # Defensive: 永不外泄异常到 LangGraph
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                try:
                    await rollback()
                except Exception:  # pragma: no cover - defensive cleanup
                    logger.warning("ToolAdapter.execute: rollback failed", exc_info=True)
            finished_at = self._clock()
            duration_ms = self._elapsed_ms(started_at, finished_at)
            logger.exception("ToolAdapter.execute: tool %s raised", tool_name)
            envelope = self._unexpected_error_envelope(
                tool_name=tool_name, attempt=attempt, exc=exc,
                task_id=str(ctx_runtime.task_internal_id),
                duration_ms=duration_ms,
            )
        else:
            finished_at = self._clock()
            duration_ms = self._elapsed_ms(started_at, finished_at)
            # Legacy 在 result 上挂 task_id 与 duration_ms(orchestrator 内)
            result = dict(result or {})
            result.setdefault("task_id", str(ctx_runtime.task_internal_id))
            result["duration_ms"] = duration_ms
            result.setdefault("attempt", attempt)
            # Phase 2.9B.6: Tool Identity Contract —
            # 把真实 tool_call_id 写回正式 Tool Execution Envelope,
            # 保证 ``envelope.tool_call_id == tool_started.tool_call_id``。
            # 节点据此构造 PendingNarrative.source_tool_call_id,不再使用
            # ``{tool_name}-{task_id}`` 回退串(否则前端锚定失败,LLM 叙事孤儿化)。
            result["tool_call_id"] = tool_call_id
            envelope = result
            # Phase 2.9A.9: 不再写回 ctx_runtime._intermediate_state。
            # 业务结果由各 LangGraph 节点读 ``state["..."]`` 后写回
            # Graph State(每个节点的 partial state update);Graph State
            # 经 LangGraph checkpointer 持久化到 Postgres,
            # Interrupt/Resume 后由 LangGraph 自动恢复。
            # _persist_intermediate_results 仍保留为兜底 — 只在节点没传
            # graph_state 时写入 ctx_runtime(兼容 Phase 2.1 测试 stub)。
            if graph_state is None:
                self._persist_intermediate_results(ctx_runtime, ctx_proxy)
            else:
                self._persist_graph_state_results(graph_state, ctx_proxy)

        # ── 副作用:tool_calls 行 + 多帧 TOOL_FINISHED / TOOL_FAILED
        await self._record_tool_call(
            tool_name=tool_name, inputs=inputs, envelope=envelope,
            task_internal_id=ctx_runtime.task_internal_id,
        )
        await self._emit_tool_chunks(
            tool_name=tool_name, envelope=envelope,
            tool_call_id=tool_call_id,
            ctx_runtime=ctx_runtime,
            graph_state=graph_state,
        )

        # ── min_visible_ms 兜底(Rule pacing)
        await self._pace_min_visible(started_at=started_at)

        return envelope

    async def emit_retry_event(
        self,
        *,
        tool_name: str,
        strategy: str,
        attempt: int,
        ctx_runtime: RuntimeContext,
        last_error: str = "",
        next_attempt_in_seconds: Optional[float] = None,
    ) -> None:
        """在 RETRYING 事件中走与 Legacy 一致的分帧。"""
        update = build_for_retry(
            tool_name=tool_name,
            strategy=strategy,
            attempt=attempt,
            last_error=last_error,
            next_attempt_in_seconds=next_attempt_in_seconds,
        )
        frames = split_into_chunks(update)
        for frame in frames:
            await self._sink.emit(
                task_id=str(ctx_runtime.task_internal_id),
                graph_run_id=ctx_runtime.task_internal_id
                and f"run-{ctx_runtime.task_internal_id}",
                node_name="retry_event",
                event_type="retrying",
                title=frame.headline,
                content=frame.summary,
                payload=frame.to_dict() if hasattr(frame, "to_dict") else {
                    "headline": frame.headline,
                    "summary": frame.summary,
                    "impact": frame.impact,
                    "next_action": frame.next_action,
                    "details": list(frame.details),
                    "chunk_index": frame.chunk_index,
                    "chunk_total": frame.chunk_total,
                    "chunk_final": frame.chunk_final,
                    "strategy": strategy,
                    "attempt": attempt,
                },
            )
            if self._transition_seconds > 0 and frame is not frames[-1]:
                await asyncio.sleep(self._transition_seconds)

    # ── RetryPolicy 协助:节点使用本方法判断下一步(留给 v2 节点)───────

    def decide_retry(
        self,
        *,
        tool_name: str,
        attempt: int,
        envelope: Dict[str, Any],
        current_inputs: Dict[str, Any],
    ) -> RetryDecision:
        return self._retry.decide(
            tool_name=tool_name,
            attempt=attempt,
            tool_result=envelope,
            current_inputs=current_inputs,
        )

    # ── Internals ───────────────────────────────────────────────────────

    def _build_proxy(
        self,
        *,
        tool_name: str,
        attempt: int,
        tool_call_id: str,
        ctx_runtime: RuntimeContext,
        inputs: Dict[str, Any],
        session: Any = None,
        graph_state: Optional[Dict[str, Any]] = None,
    ) -> _AgentContextProxy:
        task_public_id = _task_public_id_from_state_or_runtime(graph_state, ctx_runtime)

        async def emit_progress(progress_message: str, **metadata: Any) -> None:
            await self._emit_tool_progress(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                attempt=attempt,
                progress_message=progress_message,
                ctx_runtime=ctx_runtime,
                metadata=metadata,
            )

        proxy = _AgentContextProxy(
            task_id=task_public_id,
            conversation_id=str(ctx_runtime.conversation_internal_id),
            user_id=str(ctx_runtime.user_internal_id),
            user_internal_id=ctx_runtime.user_internal_id,
            task_internal_id=ctx_runtime.task_internal_id,
            conversation_internal_id=ctx_runtime.conversation_internal_id,
            settings_service=ctx_runtime.settings_service,
            session_factory=self._session_factory,
            session=session,
            requirement_file_id=inputs.get("requirement_file_id") or None,
            template_file_id=inputs.get("template_file_id") or None,
            user_prompt=str(inputs.get("user_prompt", "")),
            context_llm_invoker=getattr(ctx_runtime, "context_llm_invoker", None),
            runtime_context=ctx_runtime,
            llm_client=getattr(ctx_runtime, "llm_client", None),
            task_flag_resolver=getattr(ctx_runtime, "task_flag_resolver", None),
            tool_progress_emitter=emit_progress,
        )
        # Phase 2.9A.9: Graph State 是唯一业务事实源。
        # 优先级:graph_state(由 LangGraph checkpointer 持久化/恢复)
        #          > ctx_runtime._intermediate_state(Phase 2.9A.1 兜底)。
        # Interrupt/Resume 后 RuntimeContext 是新构造的,
        # 但 Graph State 已从 Postgres 恢复 — 这里读 Graph State 即得到
        # 全部预确认节点的产出。
        if graph_state is not None:
            source = graph_state
            source_label = "graph_state"
        else:
            source = getattr(ctx_runtime, "_intermediate_state", None) or {}
            source_label = "ctx._intermediate_state"
        logger.info(
            "ToolAdapter._build_proxy: source=%s | keys=%s",
            source_label,
            list(source.keys()) if source else [],
        )
        # Phase 2.9A.10: 完整业务字段传递。前置工具的产出必须传递给
        # 下游工具 — 不只是 pre-confirm 5 字段,还包括 post-confirm
        # 阶段(test_plan_content / review_result / artifact 等)。
        # 这些字段来自 Graph State(由 LangGraph checkpointer 持久化),
        # 跨节点/跨 Interrupt/Resume 仍可读。
        for field in _STATE_TRANSFER_FIELDS:
            value = source.get(field)
            if value is not None:
                setattr(proxy, field, value)
        return proxy

    def _persist_graph_state_results(
        self, graph_state: Dict[str, Any], ctx_proxy: _AgentContextProxy
    ) -> None:
        """Persist context mutations from tools back into the active graph state.

        Tools such as ``TestPlanRegenTool`` intentionally mutate
        ``context.test_plan_content`` in place. Direct sub-agent loops do not
        have a surrounding LangGraph node delta, so the adapter must copy those
        mutations back to the state object that was used to build the proxy.
        """
        try:
            changed: List[str] = []
            for field in _STATE_TRANSFER_FIELDS:
                value = getattr(ctx_proxy, field, None)
                if value is not None:
                    graph_state[field] = value
                    changed.append(field)
            logger.info(
                "ToolAdapter._persist_graph_state_results: tool=%s | fields=%s",
                ctx_proxy.tool_name if hasattr(ctx_proxy, "tool_name") else "?",
                changed,
            )
        except Exception as exc:
            logger.warning("ToolAdapter._persist_graph_state_results failed: %s", exc)

    def _persist_intermediate_results(
        self, ctx_runtime: RuntimeContext, ctx_proxy: _AgentContextProxy
    ) -> None:
        """Phase 2.9A.1: 工具执行后把 proxy 上的中间结果写回 ctx_runtime._intermediate_state。
        供下一次 _build_proxy 读取,实现工具间上下文传递。
        """
        try:
            state = getattr(ctx_runtime, "_intermediate_state", None)
            if state is None:
                # frozen+slots 不允许 setattr 新字段;但 RuntimeContext 2.9A.1 已声明
                logger.warning(
                    "ToolAdapter._persist_intermediate_results: ctx_runtime missing "
                    "_intermediate_state field, skip persist"
                )
                return
            for field in _STATE_TRANSFER_FIELDS:
                value = getattr(ctx_proxy, field, None)
                if value is not None:
                    state[field] = value
            logger.info(
                "ToolAdapter._persist_intermediate_results: tool=%s | _intermediate_state keys=%s",
                ctx_proxy.tool_name if hasattr(ctx_proxy, 'tool_name') else '?',
                list(state.keys()),
            )
        except Exception as exc:
            logger.warning("ToolAdapter._persist_intermediate_results failed: %s", exc)

    async def _record_tool_call(
        self,
        *,
        tool_name: str,
        inputs: Dict[str, Any],
        envelope: Dict[str, Any],
        task_internal_id: int,
    ) -> None:
        if self._recorder is None:
            return
        try:
            await self._recorder({
                "tool_name": tool_name,
                "inputs": inputs,
                "task_id": envelope.get("task_id"),
                "task_internal_id": task_internal_id,
                "result": envelope,
            })
        except Exception:
            logger.warning(
                "TestAgentToolAdapter: tool_calls row write failed (swallowed)",
                exc_info=True,
            )

    async def _emit_tool_chunks(
        self,
        *,
        tool_name: str,
        envelope: Dict[str, Any],
        tool_call_id: str,
        ctx_runtime: RuntimeContext,
        graph_state: Optional[Dict[str, Any]] = None,
    ) -> None:
        success = bool(envelope.get("success"))
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        error = envelope.get("error") if not success else None
        try:
            update = build_for_tool_result(
                tool_name=tool_name, success=success, data=data, error=error,
            )
            frames = split_into_chunks(update)
        except Exception:
            logger.exception("TestAgentToolAdapter: frame build failed (swallowed)")
            return

        event_type = "tool_finished" if success else "tool_failed"
        # Phase 2.9A.X: ResultReviewTool 语义失败（data.level="failed"）→ 前端
        # 应显示 FAILED 而不是 SUCCESS。ResultReviewTool._success() 永远返回
        # success=True（工具本身跑成功了），它用 data.level="failed" 表达
        # "审查未通过"。这里识别这种情况并把事件类型升级为 tool_failed，
        # 让 useTaskEvents.ts 把这条消息渲染成红色 FAILED 标签。
        if (
            success
            and tool_name == "ResultReviewTool"
            and isinstance(data, dict)
            and str(data.get("level") or "") == "failed"
        ):
            event_type = "tool_failed"
        narrative_expected = _tool_narrative_expected()
        # Phase 2.9A.13: 同一个 (task_id, tool_name, attempt) 只允许一个
        # tool_failed 终态事件。前端事件流用 chunk_index/chunk_final 做进度
        # 渲染,但 tool_failed 这种"已经是终态"的事件若拆成多帧会导致
        # 重复计数,误以为发生了多次失败。这里:
        #   - tool_finished: 保留多帧(成功路径上分步展示有 UX 价值)
        #   - tool_failed / tool_retry: 只发最后一帧(终态),丢弃中间帧
        if not success and event_type in ("tool_failed", "tool_retry") and len(frames) > 1:
            frames = [frames[-1]]
        terminal_event_id: Optional[str] = None
        for frame in frames:
            payload = frame.to_dict() if hasattr(frame, "to_dict") else {
                "headline": frame.headline,
                "summary": frame.summary,
                "impact": frame.impact,
                "next_action": frame.next_action,
                "details": list(frame.details),
                "chunk_index": frame.chunk_index,
                "chunk_total": frame.chunk_total,
                "chunk_final": frame.chunk_final,
            }
            # The UI tool log consumes a compact output field.  Keep it aligned
            # with the already-sanitised public narrative instead of exposing
            # the legacy tool envelope's raw result.
            public_output = build_tool_display_output(frame, success=success)
            payload["display_output"] = public_output
            payload["output"] = public_output
            payload["output_summary"] = public_output
            payload["duration_ms"] = envelope.get("duration_ms", 0)
            payload["attempt"] = envelope.get("attempt", 1)
            payload["tool_name"] = tool_name
            payload["tool_call_id"] = tool_call_id
            payload["display_tool_name"] = _display_tool_name(tool_name)
            payload["completion_message"] = (
                _completion_message(
                    tool_name,
                    data,
                    public_output,
                    graph_state=graph_state,
                )
                if success
                else ""
            )
            payload["error_summary"] = (
                _safe_tool_error_summary(tool_name, error)
                if not success
                else _semantic_failure_summary(tool_name, data)
            )
            payload["narrative_expected"] = narrative_expected
            _apply_dynamic_agent_public_update_override(
                payload,
                tool_name=tool_name,
                graph_state=graph_state,
            )
            emitted = await self._sink.emit(
                task_id=str(ctx_runtime.task_internal_id),
                graph_run_id=f"run-{ctx_runtime.task_internal_id}",
                node_name=tool_name,
                event_type=event_type,
                title=frame.headline,
                content=frame.summary,
                payload=payload,
            )
            # Phase 2.9B.6: 捕获最终 terminal 事件的真实 event_id,
            # 供节点构造 PendingNarrative.source_event_id(不再写空串)。
            # 多帧成功路径上只取最后一个 chunk_final=true 的事件。
            if isinstance(emitted, dict):
                event_id = emitted.get("event_id")
                if isinstance(event_id, str) and event_id:
                    terminal_event_id = event_id
            if self._transition_seconds > 0 and frame is not frames[-1]:
                await asyncio.sleep(self._transition_seconds)
        # 记录本次执行的终态事件(供节点在 execute 返回后读取真实 event_id)。
        self._last_terminal_event_id = terminal_event_id

    async def _emit_tool_started(
        self,
        *,
        tool_name: str,
        inputs: Dict[str, Any],
        attempt: int,
        tool_call_id: str,
        ctx_runtime: RuntimeContext,
    ) -> None:
        """Emit the public start frame before the tool can produce a result."""
        display_input = build_tool_display_input(tool_name, inputs)
        await self._sink.emit(
            task_id=str(ctx_runtime.task_internal_id),
            graph_run_id=f"run-{ctx_runtime.task_internal_id}",
            node_name=tool_name,
            event_type="tool_started",
            title=f"{tool_name} 开始执行",
            content="",
            payload={
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "attempt": attempt,
                "display_tool_name": _display_tool_name(tool_name),
                "progress_message": _start_progress_message(tool_name),
                "display_input": display_input,
                "input": display_input,
                "input_keys": sorted(inputs),
                "narrative_expected": _tool_narrative_expected(),
            },
        )

    async def _emit_tool_progress(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        attempt: int,
        progress_message: str,
        ctx_runtime: RuntimeContext,
        metadata: Dict[str, Any],
    ) -> None:
        message = str(progress_message or "").strip()
        if not message:
            return
        payload: Dict[str, Any] = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "attempt": attempt,
            "display_tool_name": _display_tool_name(tool_name),
            "progress_message": message,
            "narrative_expected": _tool_narrative_expected(),
        }
        for key in (
            "business_action",
            "business_subject_type",
            "business_subject_name",
            "file_public_id",
            "file_name",
        ):
            value = metadata.get(key)
            if value:
                payload[key] = str(value)
        await self._sink.emit(
            task_id=str(ctx_runtime.task_internal_id),
            graph_run_id=f"run-{ctx_runtime.task_internal_id}",
            node_name=tool_name,
            event_type="tool_progress",
            title=message,
            content=message,
            payload=payload,
        )

    async def _pace_min_visible(self, *, started_at: datetime) -> None:
        if self._min_visible_seconds <= 0:
            return
        elapsed = (self._clock() - started_at).total_seconds()
        remaining = self._min_visible_seconds - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

    @staticmethod
    def _elapsed_ms(started_at: datetime, finished_at: datetime) -> int:
        delta = (finished_at - started_at).total_seconds() * 1000.0
        return max(0, int(delta))

    @staticmethod
    def _missing_inputs_envelope(
        *,
        tool_name: str,
        attempt: int,
        missing: List[str],
        task_id: str,
    ) -> Dict[str, Any]:
        return {
            "success": False,
            "tool_name": tool_name,
            "task_id": task_id,
            "data": None,
            "summary": f"缺少必填字段: {', '.join(missing)}",
            "warnings": [],
            "error": {
                "code": "INVALID_INPUTS",
                "message": f"missing required keys: {', '.join(missing)}",
                "recoverable": False,
            },
            "duration_ms": 0,
            "attempt": attempt,
        }

    @staticmethod
    def _unexpected_error_envelope(
        *,
        tool_name: str,
        attempt: int,
        exc: BaseException,
        task_id: str,
        duration_ms: int,
    ) -> Dict[str, Any]:
        return {
            "success": False,
            "tool_name": tool_name,
            "task_id": task_id,
            "data": None,
            "summary": f"{tool_name} 执行异常",
            "warnings": [],
            "error": {
                "code": "TOOL_EXECUTION_ERROR",
                "message": str(exc),
                "recoverable": False,
            },
            "duration_ms": duration_ms,
            "attempt": attempt,
        }


def _task_public_id_from_state_or_runtime(
    graph_state: Optional[Dict[str, Any]],
    ctx_runtime: RuntimeContext,
) -> str:
    if graph_state is not None:
        task_id = graph_state.get("task_id")
        if task_id:
            return str(task_id)
    runtime_task_id = getattr(ctx_runtime, "task_id", None)
    if runtime_task_id:
        return str(runtime_task_id)
    return str(ctx_runtime.task_internal_id)


def _tool_narrative_expected() -> bool:
    try:
        from app.agent_runtime.feature_flags import get_feature_flags

        flags = get_feature_flags()
        return bool(
            flags.phase29b_tool_narrative_enabled
            and flags.phase29b_tool_narrative_blocking_enabled
        )
    except Exception:
        return False


def _display_tool_name(tool_name: str) -> str:
    names = {
        "RequirementParserTool": "调用Word文档解析工具",
        "TemplateParserTool": "调用模板解析工具",
        "KnowledgeSearchTool": "调用知识库查询工具",
        "SectionSuggestionTool": "调用章节建议工具",
        "TestPlanGeneratorTool": "调用测试方案生成工具",
        # Phase 2.9A.X: 补全后置 3 个工具的中文名 — 否则
        # payload.display_tool_name fallback 到英文，前端继续显示
        # ``ResultReviewTool`` 等原始名称而不是中文友好名。
        "ResultReviewTool": "调用结果审查工具",
        "WordExportTool": "调用Word文档导出工具",
        "DocxFormatCheckTool": "调用Word文档格式检查工具",
    }
    return names.get(tool_name, tool_name)


def _start_progress_message(tool_name: str) -> str:
    messages = {
        "RequirementParserTool": "正在准备解析 Word 文档",
        "TemplateParserTool": "正在准备解析模板",
        "KnowledgeSearchTool": "正在准备查询知识库",
        "SectionSuggestionTool": "正在准备生成章节建议",
        "TestPlanGeneratorTool": "正在准备生成测试方案",
    }
    return messages.get(tool_name, "工具正在运行")


def _completion_message(
    tool_name: str,
    data: Dict[str, Any],
    fallback: str,
    *,
    graph_state: Optional[Dict[str, Any]] = None,
) -> str:
    if tool_name == "RequirementParserTool":
        file_name = str(data.get("document_name") or data.get("file_name") or "").strip()
        return f"文档《{file_name}》已解析完成" if file_name else "Word 文档已解析完成"
    if tool_name == "TemplateParserTool":
        file_name = str(data.get("template_name") or data.get("file_name") or "").strip()
        return f"模板《{file_name}》已解析完成" if file_name else "模板已解析完成"
    if tool_name == "KnowledgeSearchTool":
        project_result = (
            graph_state.get("_project_rag_display")
            if isinstance(graph_state, dict)
            else None
        )
        return _knowledge_search_completion_message(data, project_result=project_result)
    if tool_name == "SectionSuggestionTool":
        return "章节建议已生成完成"
    if tool_name == "TestPlanGeneratorTool":
        return "测试方案内容已生成完成"
    return fallback


def _company_knowledge_search_completion_message(data: Dict[str, Any]) -> str:
    """Describe the actual Company RAG outcome instead of a generic success.

    A missing Company RAG configuration is intentionally non-blocking, so the
    tool envelope remains successful.  The public card must nevertheless say
    that no company knowledge was used.
    """
    skip_reason = str(data.get("skip_reason") or "").strip().lower()
    error_code = str(data.get("error_code") or "").strip().upper()
    if error_code == "KNOWLEDGE_NOT_CONFIGURED" or skip_reason in {
        "not_configured",
        "knowledge_not_configured",
    }:
        return "公司知识库未配置，已跳过；将仅依据需求与项目资料继续。"
    if data.get("disabled_by_config"):
        return "公司知识库已禁用，已跳过；将仅依据需求与项目资料继续。"
    if data.get("degraded"):
        return "公司知识库暂不可用，已降级继续；未将其作为测试方案依据。"
    if skip_reason:
        return "公司知识库本次已跳过；将仅依据需求与项目资料继续。"

    chunks = data.get("chunks")
    hit_count = data.get("hit_count")
    if (isinstance(chunks, list) and not chunks) or hit_count == 0:
        return "公司知识库已查询，但未命中可引用内容。"
    return "公司知识库查询完成，已整理可引用内容。"


def _knowledge_search_completion_message(
    data: Dict[str, Any],
    *,
    project_result: Optional[Dict[str, Any]] = None,
) -> str:
    """Render Company and Project retrieval as two explicit source outcomes."""
    company_message = _company_knowledge_search_completion_message(data)
    if not isinstance(project_result, dict):
        return company_message
    return f"{company_message}\n{_project_rag_completion_message(project_result)}"


def _project_rag_completion_message(project_result: Dict[str, Any]) -> str:
    status = str(project_result.get("status") or "")
    hits = project_result.get("hits")
    hit_count = len(hits) if isinstance(hits, list) else 0
    if status == "completed":
        if hit_count:
            return f"项目资料检索已完成，命中 {hit_count} 条可引用内容。"
        return "项目资料已检索，但未命中可引用内容。"
    if status == "degraded":
        return "项目资料检索暂不可用，已降级继续。"
    if status == "skipped" and project_result.get("reason") == "no_project":
        return "当前会话未关联项目，项目资料检索已跳过。"
    if status == "skipped":
        return "项目资料检索本次已跳过。"
    return "项目资料检索状态未知，未将其作为测试方案依据。"


def _apply_dynamic_agent_public_update_override(
    payload: Dict[str, Any],
    *,
    tool_name: str,
    graph_state: Optional[Dict[str, Any]],
) -> None:
    if tool_name != "RequirementParserTool" or not isinstance(graph_state, dict):
        return
    if str(graph_state.get("target_capability") or "") != "document_qa":
        return
    operation = str(graph_state.get("operation") or "")
    headline = str(payload.get("headline") or "")
    if "\u6d4b\u8bd5\u65b9\u6848" in headline:
        payload["headline"] = "\u5df2\u5b8c\u6210 Word \u6587\u6863\u89e3\u6790"
    payload["impact"] = (
        "\u89e3\u6790\u7ed3\u679c\u5c06\u4f5c\u4e3a\u540e\u7eed\u6587\u6863"
        "\u5206\u6790\u548c\u7b54\u590d\u751f\u6210\u7684\u8bc1\u636e\u3002"
    )
    action = (
        "\u63a5\u4e0b\u6765\u5c06\u5206\u6790\u6587\u6863\u8bc1\u636e\u3002"
        if operation in {"analyze", "summarize", "extract", "compare"}
        else "\u63a5\u4e0b\u6765\u5c06\u57fa\u4e8e\u6587\u6863\u8bc1\u636e"
        "\u5904\u7406\u4f60\u7684\u95ee\u9898\u3002"
    )
    payload["next_action"] = action
    payload["nextAction"] = action


def _semantic_failure_summary(tool_name: str, data: Any) -> str:
    """Phase 2.9A.X: 工具返回 success=True 但语义失败（典型如
    ResultReviewTool data.level="failed"）— 拼接分点明细作为 error_summary。
    前端 buildToolCallPresentation 会用它替代通用 fallback 文案。
    """
    if not isinstance(data, dict):
        return ""
    # 仅 ResultReviewTool 等有"语义失败"概念（level 区分）
    if tool_name != "ResultReviewTool":
        return ""
    if str(data.get("level") or "") != "failed":
        return ""
    block = data.get("block_issues") or data.get("issues") or []
    if not isinstance(block, list) or not block:
        return "审查未通过，接下来进入 RepairAgent。"
    lines = ["审查未通过，结果不符合："]
    for idx, item in enumerate(block[:8], start=1):
        if not isinstance(item, dict):
            continue
        msg = item.get("message") or item.get("rule_id") or "未通过"
        sid = item.get("section_id")
        prefix = f"{idx}. " if sid is None else f"{idx}. [{sid}] "
        lines.append(f"{prefix}{msg}")
    lines.append("接下来进入 RepairAgent 定点修复。")
    return "\n".join(lines)


def _safe_tool_error_summary(tool_name: str, error: Any) -> str:
    code = ""
    if isinstance(error, dict):
        code = str(error.get("code") or "")
    by_code = {
        "REQUIREMENT_FILE_NOT_FOUND": "文件已不存在，请重新上传后重试",
        "REQUIREMENT_PARSE_FAILED": "无法读取该 Word 文档，请确认文件未损坏",
        "TEMPLATE_FILE_NOT_FOUND": "模板文件已不存在，请重新上传后重试",
        "TEMPLATE_PARSE_FAILED": "模板解析失败，请确认文件格式正确",
        "KNOWLEDGE_API_UNAVAILABLE": "知识库暂时不可用，请稍后重试",
        "KNOWLEDGE_NOT_CONFIGURED": "知识库尚未配置，请先完成配置",
        "MODEL_CONFIG_ERROR": "模型尚未配置，请先完成模型配置",
        "MODEL_TIMEOUT": "模型生成暂时超时，请稍后重试",
        "JSON_VALIDATION_FAILED": "模型返回内容未通过结构校验，系统将尝试重试",
    }
    if code in by_code:
        return by_code[code]
    if tool_name == "RequirementParserTool":
        return "工具执行失败，请查看后端日志或重新上传文件"
    if tool_name == "TemplateParserTool":
        return "模板解析失败，请查看后端日志或检查模板文件"
    if tool_name == "KnowledgeSearchTool":
        return "知识库查询失败，请查看后端日志"
    if tool_name == "SectionSuggestionTool":
        return "章节建议生成失败，请查看后端日志"
    if tool_name == "TestPlanGeneratorTool":
        return "测试方案生成失败，请查看后端日志"
    return "工具执行失败，请查看后端日志"


__all__ = [
    "DEFAULT_TOOL_WHITELIST",
    "TestAgentToolAdapter",
    "ToolExecutorLike",
    "UnknownToolError",
    "ToolAdapterError",
]
