"""Phase 2.9B.4 — NarrativeComposer 横切服务。

职责(docs/93 技术方案 §八):
* 构造模型 Prompt(Tagged Narrative Stream V1);
* 调用真实 LLMClient 流式接口;
* 接收 token stream → NarrativeStreamDecoder;
* 发 delta 事件(写入 DB + LiveEventBus);
* 组装 PublicExecutionUpdate;
* Schema 校验 + 事实校验;
* 必要时执行一次修复(fact_feedback);
* 失败时返回确定性 fallback;
* 不修改 Graph 业务状态。

同步屏障语义由调用方(Narrative Barrier Node)保证 —— 本服务只负责生成与
事件持久化,绝不 create_task 后台生成。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

from app.agent.enums import AgentEventType
from app.agent_runtime._shared.public_narrative import (
    AgentPublicUpdateDraft,
    build_narrative_envelope,
)
from app.llm.task_profiles import (
    NARRATIVE_SCHEMA_REPAIR_PROFILE,
    TOOL_NARRATIVE_COMPOSER_PROFILE,
)

from .prompts import (
    TAGGED_NARRATIVE_OUTPUT_CONTRACT,
    build_repair_prompt,
    build_tool_narrative_prompt,
)
from .schemas import (
    NarrativeGenerationRequest,
    NarrativeGenerationResult,
    NarrativeSource,
    NarrativeStreamChunk,
    NarrativeValidationResult,
    TaskSummaryNarrativeContext,
    ToolNarrativeContext,
)
from .stream_decoder import NarrativeStreamDecoder, StreamDecodeError
from .validator import NarrativeValidator

logger = logging.getLogger(__name__)

_DELTA_FLUSH_CHARS = 32
_DELTA_FLUSH_MS = 0.15


class NarrativeComposer:
    """横切叙事服务 — 生成、校验、修复、回退。

    实例化时由 barrier / task_summary 节点注入:
      - ``llm_client``: 流式 LLM 客户端(必须有 stream_with_system 或 generate_with_system)
      - ``event_sink``: SSE 推流(用于发 started/delta/update/failed/fallback 事件)
      - ``validator``: Schema + 事实校验器(可注入 mock 用于测试)
      - ``deterministic_fallback``: 失败时返回的固定字段,draft 形态
      - ``timeout_seconds``: 整体超时(默认 45s)
      - ``repair_attempts``: 一次生成失败后允许修复重试的次数(默认 1)

    设计约束: 同步屏障语义由 barrier 节点保证,本服务只负责"生成 + 发事件
    + 校验 + 修复 + 兜底",绝不 create_task 后台生成。
    """

    def __init__(
        self,
        llm_client: Any,
        event_sink: Any,
        *,
        validator: Optional[NarrativeValidator] = None,
        deterministic_fallback: Optional[AgentPublicUpdateDraft] = None,
        timeout_seconds: float = 45.0,
        repair_attempts: int = 1,
    ):
        self._llm = llm_client
        self._sink = event_sink
        self._validator = validator or NarrativeValidator()
        # deterministic_fallback 若 None,失败时不发 fallback event;
        # 真正生产链路总是传(barrier 注入 _DETERMINISTIC_TOOL_FALLBACK)。
        self._deterministic_fallback = deterministic_fallback
        self._timeout_seconds = timeout_seconds
        self._repair_attempts = max(0, repair_attempts)
        # _pending_result: 一次 _run_generation 内部的中间态,以便异常分支
        # 还能回填 fallback(避免返回空 dict 让调用方不知道发生了什么)。
        self._pending_result: Optional[NarrativeGenerationResult] = None

    # ── 对外入口(Tool 叙事)──────────────────────────────────────────────

    async def compose_tool_narrative(
        self,
        *,
        task_internal_id: int,
        graph_run_id: str,
        context: ToolNarrativeContext,
        narrative_id: str,
        generation_id: str,
        generation_no: int,
        event_prefix: str = "tool",
    ) -> NarrativeGenerationResult:
        """同步生成 Tool 叙事;返回终态结果。调用方(Barrier)必须 await。

        工作流:
          1) 构造 (system, user) prompt;
          2) 包装 NarrativeGenerationRequest(kind="tool");
          3) 走 ``_run_generation``: 流式 LLM → decoder → Schema+事实校验 → 可选一次 repair → fallback;
          4) 返回 NarrativeGenerationResult(source=llm/deterministic)。

        barrier 调用方处理返回结果: source=="llm" 走 SSE update,
        source=="deterministic" 走 fallback(update 的 narrative_source=deterministic)。
        """
        # prompts.py 锁定不能改 — 这里只是调用,封装好让 barrier 调用方更简单。
        system_prompt, user_content = build_tool_narrative_prompt(context)
        request = NarrativeGenerationRequest(
            kind="tool",
            narrative_id=narrative_id,
            generation_id=generation_id,
            generation_no=generation_no,
            tool_context=context,                # 关键: 让 _run_generation 知道是 Tool 叙事
            system_prompt=system_prompt,
            user_content=user_content,
        )
        # event_prefix="tool" 让事件类型选 TOOL_NARRATIVE_*;task_summary 时是 "task_summary"。
        return await self._run_generation(
            task_internal_id=task_internal_id,
            graph_run_id=graph_run_id,
            request=request,
            context_for_fallback=context,
            event_prefix=event_prefix,
            profile=TOOL_NARRATIVE_COMPOSER_PROFILE,
        )

    # ── 修复调用 ─────────────────────────────────────────────────────────

    async def _repair_generation(
        self,
        *,
        task_internal_id: int,
        graph_run_id: str,
        request: NarrativeGenerationRequest,
        feedback: str,
        event_prefix: str,
        profile: Any,
        original_output: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """一次带 fact_feedback 的修复调用;返回 public_update dict 或 None。

        工作流:
          1) 把原始 ToolNarrativeContext / TaskSummaryContext 渲染成精简事实 + 约束白名单;
          2) 用 build_repair_prompt(``feedback`` + 原始输出 + 事实) 构造新 prompt;
          3) 重新流式调用 LLM,经过 NarrativeStreamDecoder 解析;
          4) 失败 → 返回 None,主路径走确定性 fallback。

        Phase 2.9B.5: 修复 Prompt 携带原始输出 + 精简事实 + 约束白名单,
        让修复模型能理解失败原因(如「文件名内嵌数字被误判」)。
        """
        # 三类请求共用同一组修复逻辑,只是 context 来源不同(tool / task_summary / 无)。
        if request.tool_context:
            # tool 叙事: 把 input_facts / output_facts / execution_context 序列化。
            context_json = json.dumps(
                {
                    "tool_name": request.tool_context.tool_name,
                    "terminal_status": request.tool_context.terminal_status,
                    "input_facts": request.tool_context.input_facts,
                    "output_facts": request.tool_context.output_facts,
                    "execution_context": request.tool_context.execution_context,
                },
                ensure_ascii=False,
                default=str,
            )
            constraints = request.tool_context.fact_constraints
        elif request.task_summary_context:
            # task summary 叙事: 完整 dump(去除 fact_constraints,它是 Validator 的事)。
            context_json = json.dumps(
                request.task_summary_context.model_dump(
                    exclude={"fact_constraints"},
                ),
                ensure_ascii=False,
                default=str,
            )
            constraints = request.task_summary_context.fact_constraints
        else:
            context_json = "{}"
            constraints = None
        # 把白名单渲染给修复模型,让它重新生成时能遵守同样的事实约束。
        constraints_json = (
            json.dumps(
                {
                    "allowed_numeric_facts": constraints.allowed_numeric_facts,
                    "allowed_file_names": constraints.allowed_file_names,
                    "allowed_literal_facts": constraints.allowed_literal_facts,
                    "numeric_exempt_literals": constraints.numeric_exempt_literals,
                },
                ensure_ascii=False,
                default=str,
            )
            if constraints
            else "{}"
        )
        # 原始模型输出也带上,让修复模型能"看到自己第一次写的"以理解错点。
        system_prompt, user_content = build_repair_prompt(
            feedback=feedback,
            context_json=context_json,
            original_output=json.dumps(original_output or {}, ensure_ascii=False, default=str),
            constraints_json=constraints_json,
        )
        decoder = NarrativeStreamDecoder()
        try:
            # 重新调一次 LLM,同样用流式 + 同样的解码器。
            async for chunk in self._stream_llm(
                system_prompt,
                user_content,
                profile=profile,
                output_contract=TAGGED_NARRATIVE_OUTPUT_CONTRACT,
            ):
                decoder.feed(chunk)
            decoder.finish()
            return decoder.public_update_dict()
        except (StreamDecodeError, Exception) as exc:  # noqa: BLE001
            # 修复也失败就放弃,主路径 fallback ——
            # 这是为了避免"为了写好叙事二次重试"反而成为任务卡死的原因。
            logger.warning(
                "narrative repair failed | narrative_id=%s | err=%s",
                request.narrative_id, exc,
            )
            return None

    async def _stream_llm(
        self,
        system_prompt: str,
        user_content: str,
        *,
        profile: Any = None,
        output_contract: str = TAGGED_NARRATIVE_OUTPUT_CONTRACT,
    ) -> AsyncIterator[str]:
        """流式调用 LLMClient。适配 generate_with_profile + stream 或纯 stream。

        LLMClient 暴露 ``stream_with_system`` 即流式;否则退到一次性
        ``generate_with_system`` 把整段当一个 chunk yield 出去。
        """
        if not getattr(self._llm, "is_context_engine_bridge", False):
            raise RuntimeError("MIGRATION_CONTEXT_REQUIRED")
        if hasattr(self._llm, "stream_with_system"):
            # 优选流式: 边来边喂 NarrativeStreamDecoder,实现"边写边展示"。
            async for chunk in self._llm.stream_with_system(
                system_prompt,
                user_content,
                timeout_override=int(self._timeout_seconds),
                llm_task_profile=profile,
                output_contract=output_contract,
            ):
                yield chunk
            return
        # fallback: 非流式一次性返回。
        result = await self._llm.generate_with_system(
            system_prompt,
            user_content,
            timeout_override=int(self._timeout_seconds),
            llm_task_profile=profile,
            output_contract=output_contract,
        )
        yield result

    # ── 核心生成循环 ─────────────────────────────────────────────────────

    async def _run_generation(
        self,
        *,
        task_internal_id: int,
        graph_run_id: str,
        request: NarrativeGenerationRequest,
        context_for_fallback: Any,
        event_prefix: str,
        profile: Any,
    ) -> NarrativeGenerationResult:
        """流式生成 + 校验 + 修复;返回终态结果(调用方必须 await)。"""
        result = NarrativeGenerationResult(
            narrative_id=request.narrative_id,
            generation_id=request.generation_id,
            generation_no=request.generation_no,
            success=False,
            source="deterministic",
            status="fallback",
            fallback_used=True,
            failure_category="invalid_model_narrative",
        )

        try:
            logger.info(
                "narrative generation start | kind=%s | narrative_id=%s | generation_id=%s | task=%s | tool=%s | source_tool_call_id=%s | source_event_id=%s",
                request.kind,
                request.narrative_id,
                request.generation_id,
                task_internal_id,
                request.tool_context.tool_name if request.tool_context else "",
                request.tool_context.tool_call_id if request.tool_context else "",
                request.tool_context.source_event_id if request.tool_context else "",
            )
            await asyncio.wait_for(
                self._generate_with_stream(
                    task_internal_id, graph_run_id, request, event_prefix, profile
                ),
                timeout=self._timeout_seconds,
            )
            return self._pending_result
        except asyncio.TimeoutError:
            logger.warning("narrative timeout | narrative_id=%s", request.narrative_id)
            result.failure_category = "timeout"
            from app.agent_runtime.feature_flags import get_feature_flags
            if get_feature_flags().phase29b_deterministic_fallback_enabled:
                result.public_update = self._deterministic_fallback
            await self._emit_failed(
                task_internal_id, graph_run_id, request, event_prefix,
                category="timeout",
            )
            await self._emit_fallback(
                task_internal_id, graph_run_id, request, event_prefix,
                category="timeout",
            )
            return result

    async def _generate_with_stream(
        self,
        task_internal_id: int,
        graph_run_id: str,
        request: NarrativeGenerationRequest,
        event_prefix: str,
        profile: Any,
    ) -> None:
        """流式生成 + 校验 + 修复;结果写入 self._pending_result。"""
        system_prompt = request.system_prompt
        user_content = request.user_content
        decoder = NarrativeStreamDecoder()
        deltas: List[NarrativeStreamChunk] = []
        buffered: List[NarrativeStreamChunk] = []
        raw_chunks: List[str] = []

        async def flush_deltas() -> None:
            if not buffered:
                return
            await self._emit_deltas(
                task_internal_id, graph_run_id, request, event_prefix, buffered
            )
            buffered.clear()

        self._pending_result = None
        try:
            await self._emit_started(task_internal_id, graph_run_id, request, event_prefix)
            async for chunk in self._stream_llm(
                system_prompt,
                user_content,
                profile=profile,
                output_contract=TAGGED_NARRATIVE_OUTPUT_CONTRACT,
            ):
                raw_chunks.append(chunk)
                new_deltas = decoder.feed(chunk)
                if new_deltas:
                    buffered.extend(new_deltas)
                    deltas.extend(new_deltas)
                    if len(buffered) >= 3:
                        await flush_deltas()
            decoder.finish()
            await flush_deltas()
            logger.info(
                "narrative llm stream completed | kind=%s | narrative_id=%s | generation_id=%s | raw_chars=%s | delta_count=%s",
                request.kind,
                request.narrative_id,
                request.generation_id,
                sum(len(c) for c in raw_chunks),
                len(deltas),
            )
        except StreamDecodeError as exc:
            raw_output = "".join(raw_chunks)
            logger.warning(
                "narrative decode error | kind=%s | narrative_id=%s | err=%s | raw_chars=%s",
                request.kind,
                request.narrative_id,
                exc,
                len(raw_output),
            )
            # A non-empty model response that misses only the tagged wrapper is
            # recoverable.  Previously this branch emitted the deterministic
            # fallback before the existing schema-repair path had a chance to
            # correct the response, so task summaries silently lost valid LLM
            # prose whenever the provider returned bare Markdown.
            if self._repair_attempts > 0 and raw_output.strip():
                feedback = (
                    "输出不符合 Tagged Natural Narrative V2："
                    f"{exc}。请只返回一个完整的 <NARRATIVE>...</NARRATIVE> 块。"
                )
                logger.info(
                    "narrative decode repair attempt start | kind=%s | narrative_id=%s | raw_chars=%s",
                    request.kind,
                    request.narrative_id,
                    len(raw_output),
                )
                repaired = await self._repair_generation(
                    task_internal_id=task_internal_id,
                    graph_run_id=graph_run_id,
                    request=request,
                    feedback=feedback,
                    event_prefix=event_prefix,
                    profile=NARRATIVE_SCHEMA_REPAIR_PROFILE,
                    original_output={"raw_model_output": raw_output},
                )
                if repaired:
                    validation = self._validator.validate(
                        public_update=repaired,
                        tool_context=request.tool_context,
                        task_summary_context=request.task_summary_context,
                    )
                    logger.info(
                        "narrative decode repair attempt result | kind=%s | narrative_id=%s | valid=%s | errors=%s",
                        request.kind,
                        request.narrative_id,
                        validation.valid,
                        validation.errors[:4],
                    )
                    if validation.valid:
                        await self._complete_valid_generation(
                            task_internal_id=task_internal_id,
                            graph_run_id=graph_run_id,
                            request=request,
                            event_prefix=event_prefix,
                            public_update=repaired,
                            deltas=[],
                        )
                        return
            await self._emit_failed(
                task_internal_id, graph_run_id, request, event_prefix,
                category="stream_decode_error",
                raw_output_length=len(raw_output),
                delta_count=len(deltas),
            )
            self._pending_result = self._fallback_result_from(
                request, "stream_decode_error"
            )
            await self._emit_fallback(
                task_internal_id, graph_run_id, request, event_prefix,
                category="stream_decode_error",
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("narrative llm error | err=%s", exc)
            await self._emit_failed(
                task_internal_id, graph_run_id, request, event_prefix,
                category="llm_error",
            )
            self._pending_result = self._fallback_result_from(
                request, "llm_error"
            )
            await self._emit_fallback(
                task_internal_id, graph_run_id, request, event_prefix,
                category="llm_error",
            )
            return

        public_update = decoder.public_update_dict()
        raw_output_length = len(json.dumps(public_update, ensure_ascii=False, default=str))
        validation = self._validator.validate(
            public_update=public_update,
            tool_context=request.tool_context,
            task_summary_context=request.task_summary_context,
        )
        logger.info(
            "narrative validation result | kind=%s | narrative_id=%s | generation_id=%s | valid=%s | errors=%s | raw_output_length=%s | delta_count=%s | has_narrative_text=%s",
            request.kind,
            request.narrative_id,
            request.generation_id,
            validation.valid,
            validation.errors[:4],
            raw_output_length,
            len(deltas),
            bool(str(public_update.get("narrative_text") or "").strip()),
        )
        if not validation.valid and self._repair_attempts > 0:
            logger.info(
                "narrative repair attempt start | kind=%s | narrative_id=%s | generation_id=%s | feedback=%s",
                request.kind,
                request.narrative_id,
                request.generation_id,
                validation.fact_feedback,
            )
            repaired = await self._repair_generation(
                task_internal_id=task_internal_id,
                graph_run_id=graph_run_id,
                request=request,
                feedback=validation.fact_feedback or ";".join(validation.errors),
                event_prefix=event_prefix,
                profile=NARRATIVE_SCHEMA_REPAIR_PROFILE,
                original_output=public_update,
            )
            if repaired:
                validation = self._validator.validate(
                    public_update=repaired,
                    tool_context=request.tool_context,
                    task_summary_context=request.task_summary_context,
                )
                if validation.valid:
                    public_update = repaired
                    raw_output_length = len(json.dumps(public_update, ensure_ascii=False, default=str))
            logger.info(
                "narrative repair attempt result | kind=%s | narrative_id=%s | generation_id=%s | valid=%s | errors=%s",
                request.kind,
                request.narrative_id,
                request.generation_id,
                validation.valid,
                validation.errors[:4],
            )

        if validation.valid:
            await self._complete_valid_generation(
                task_internal_id=task_internal_id,
                graph_run_id=graph_run_id,
                request=request,
                event_prefix=event_prefix,
                public_update=public_update,
                deltas=deltas,
            )
        else:
            await self._emit_failed(
                task_internal_id, graph_run_id, request, event_prefix,
                category="schema_validation_failed",
                validation=validation,
                raw_output_length=raw_output_length,
                delta_count=len(deltas),
            )
            self._pending_result = self._fallback_result_from(
                request, "schema_validation_failed"
            )
            await self._emit_fallback(
                task_internal_id, graph_run_id, request, event_prefix,
                category="schema_validation_failed",
            )
            logger.warning(
                "narrative fallback emitted | kind=%s | narrative_id=%s | generation_id=%s | category=%s | errors=%s",
                request.kind,
                request.narrative_id,
                request.generation_id,
                "schema_validation_failed",
                validation.errors[:4],
            )

    async def _complete_valid_generation(
        self,
        *,
        task_internal_id: int,
        graph_run_id: str,
        request: NarrativeGenerationRequest,
        event_prefix: str,
        public_update: Dict[str, Any],
        deltas: List[NarrativeStreamChunk],
    ) -> None:
        """Persist one validated LLM narrative result and publish its final update."""
        draft = AgentPublicUpdateDraft(
            headline=str(public_update.get("headline") or "")[:80],
            summary=str(public_update.get("summary") or "")[:200],
            impact=str(public_update.get("impact") or "")[:160],
            next_action=str(public_update.get("next_action") or "")[:160],
            details=[
                str(detail)[:160]
                for detail in (public_update.get("details") or [])
                if str(detail).strip()
            ][:5],
            narrative_text=str(
                public_update.get("narrative_text")
                or public_update.get("narrativeText")
                or ""
            )[:800],
        )
        await self._emit_update(
            task_internal_id, graph_run_id, request, event_prefix, draft, deltas
        )
        self._pending_result = NarrativeGenerationResult(
            narrative_id=request.narrative_id,
            generation_id=request.generation_id,
            generation_no=request.generation_no,
            success=True,
            source="llm",
            status="completed",
            public_update=draft,
            failure_category=None,
            fallback_used=False,
            delta_count=len(deltas),
        )

    def _fallback_result_from(
        self,
        request: NarrativeGenerationRequest,
        category: str,
    ) -> NarrativeGenerationResult:
        return NarrativeGenerationResult(
            narrative_id=request.narrative_id,
            generation_id=request.generation_id,
            generation_no=request.generation_no,
            success=False,
            source="deterministic",
            status="fallback",
            public_update=self._deterministic_fallback,
            failure_category=category,
            fallback_used=True,
        )

    def _anchor_payload(self, request) -> Dict[str, Any]:
        """四类叙事事件共用的锚定字段(Phase 2.9B.5)。

        started / delta / update / failed 必须携带一致的
        narrative_id / generation_id / generation_no / source_event_id /
        source_tool_call_id / tool_name / attempt / schema_version /
        narrative_source / narrative_kind,前端据此把流式 partial 稳定锚定到
        对应 Tool 卡片,而不是按事件到达顺序归并。
        """
        tool_context = request.tool_context
        return {
            "narrative_id": request.narrative_id,
            "generation_id": request.generation_id,
            "generation_no": request.generation_no,
            "source_event_id": (
                tool_context.source_event_id if tool_context else ""
            ),
            "source_tool_call_id": (
                tool_context.tool_call_id if tool_context else ""
            ),
            "tool_name": (
                tool_context.tool_name if tool_context else ""
            ),
            "attempt": (
                tool_context.attempt if tool_context else 1
            ),
            "schema_version": request.schema_version,
            "narrative_source": "llm",
            "narrative_kind": request.kind,
        }

    async def _emit_started(
        self, task_internal_id, graph_run_id, request, event_prefix
    ) -> None:
        event_type = (
            AgentEventType.TOOL_NARRATIVE_STARTED.value
            if event_prefix == "tool"
            else AgentEventType.TASK_SUMMARY_NARRATIVE_STARTED.value
        )
        await self._emit(
            task_internal_id, graph_run_id, request, event_prefix,
            event_type=event_type,
            payload=self._anchor_payload(request),
        )

    async def _emit_deltas(
        self, task_internal_id, graph_run_id, request, event_prefix, chunks
    ) -> None:
        for chunk in chunks:
            event_type = (
                AgentEventType.TOOL_NARRATIVE_DELTA.value
                if event_prefix == "tool"
                else AgentEventType.TASK_SUMMARY_NARRATIVE_DELTA.value
            )
            payload = self._anchor_payload(request)
            payload.update({
                "field": chunk.field,
                "delta": chunk.delta,
                "chunk_index": chunk.chunk_index,
            })
            await self._emit(
                task_internal_id, graph_run_id, request, event_prefix,
                event_type=event_type,
                payload=payload,
            )

    async def _emit_update(
        self, task_internal_id, graph_run_id, request, event_prefix, draft, deltas
    ) -> None:
        event_type = (
            AgentEventType.TOOL_NARRATIVE_UPDATE.value
            if event_prefix == "tool"
            else AgentEventType.TASK_SUMMARY_NARRATIVE_UPDATE.value
        )
        payload = self._anchor_payload(request)
        payload.update({
            "narrative_source": "llm",
            "fallback_used": False,
            "public_update": draft.model_dump(mode="json"),
        })
        await self._emit(
            task_internal_id, graph_run_id, request, event_prefix,
            event_type=event_type,
            payload=payload,
        )

    async def _emit_failed(
        self,
        task_internal_id,
        graph_run_id,
        request,
        event_prefix,
        category,
        *,
        validation: NarrativeValidationResult | None = None,
        raw_output_length: int | None = None,
        delta_count: int | None = None,
    ) -> None:
        event_type = (
            AgentEventType.TOOL_NARRATIVE_FAILED.value
            if event_prefix == "tool"
            else AgentEventType.TASK_SUMMARY_NARRATIVE_FAILED.value
        )
        payload = self._anchor_payload(request)
        payload["failure_category"] = category
        if validation is not None:
            payload["validation_errors"] = list(validation.errors)
            if validation.fact_feedback:
                payload["fact_feedback"] = validation.fact_feedback
        if raw_output_length is not None:
            payload["raw_output_length"] = raw_output_length
        if delta_count is not None:
            payload["delta_count"] = delta_count
        await self._emit(
            task_internal_id, graph_run_id, request, event_prefix,
            event_type=event_type,
            payload=payload,
        )

    async def _emit_fallback(
        self, task_internal_id, graph_run_id, request, event_prefix, category
    ) -> None:
        from app.agent_runtime.feature_flags import get_feature_flags
        if not get_feature_flags().phase29b_deterministic_fallback_enabled:
            return
        event_type = (
            AgentEventType.TOOL_NARRATIVE_FALLBACK.value
            if event_prefix == "tool"
            else AgentEventType.TASK_SUMMARY_NARRATIVE_FALLBACK.value
        )
        payload = self._anchor_payload(request)
        payload.update({
            "narrative_source": "deterministic",
            "fallback_used": True,
            "failure_category": category,
            "fallback_reason": category,
            "public_update": (
                self._deterministic_fallback.model_dump(mode="json")
                if self._deterministic_fallback
                else None
            ),
        })
        await self._emit(
            task_internal_id, graph_run_id, request, event_prefix,
            event_type=event_type,
            payload=payload,
        )

    async def _emit(
        self, task_internal_id, graph_run_id, request, event_prefix, *, event_type, payload
    ) -> None:
        try:
            await self._sink.emit(
                task_id=str(task_internal_id),
                graph_run_id=graph_run_id,
                node_name=f"{event_prefix}_narrative_barrier",
                event_type=event_type,
                title=payload.get("headline", event_type),
                content="",
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("narrative event emit failed: %s", exc)


__all__ = ["NarrativeComposer"]
