"""ContextAwareLLMInvoker — 统一 LLM 调用入口 + Provider/Schema/ContextLength Retry。

CE-02 整改一：Invoker 迁移到 Agent Runtime 层（app/agent_runtime/context/）。
CE-02 WP-9 语义：
- 时序：compose → mark_sent → LLMClient.generate_with_profile → 解析
  usage/actual tokens → complete snapshot → parse → 返回 ContextualLLMResult；
- **Provider Error Mapping**：transport timeout/connection/status/unknown →
  SafeLLMError + 按 error 类型映射（网络/限流 → retryable；权限/400 →
  non-retryable）；旧 Snapshot failed；
- **Schema Retry**：parser 失败 → 原 Snapshot 保持 completed → 新 snapshot +
  schema_feedback 提示词 → 重试，次数由 RetryPolicy；
- **Context Length Retry 基础**：检测 context_length_error → 旧 snapshot
  failed → engine.compose_for_retry（确定性降容）→ 新 snapshot → 重试；
  最多 1-2 次，禁止无限；
- **Cancellation**：begin 前取消 → 无 Snapshot + 安全 Trace（不调 LLM）；
  begin 后 sent 前取消 → abandon；sent 后取消 → 尝试取消 Provider，按真实
  结果 completed/failed/abandoned。取消**不得转为 degraded warning**。

CE-02 整改二：provider_request_id **不写入 latency_json**（latency_json 严格
映射 ContextBuildLatency，只含延迟字段）；provider_request_id 保留在
ContextualLLMResult 与 LLMAttemptRef，Snapshot DB 无独立字段时不持久化。
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Generic, TypeVar

from app.agent_runtime.context.provider_error_mapper import (
    extract_usage,
    llm_latency_ms,
    map_provider_error,
    parse_result,
    provider_request_id,
)
from app.agent_runtime.context.retry_policy import RetryPolicy
from app.context_engine.errors import ContextEngineStage, raise_engine_error
from app.context_engine.models.context import ContextRequest
from app.context_engine.models.retry_models import LLMAttemptRef, SafeLLMError
from app.context_engine.models.snapshot_models import (
    ContextSnapshotCompleteCommand,
    ContextSnapshotFailCommand,
    ContextSnapshotRef,
)
from app.context_engine.models.state_ref_builder import build_context_state_ref

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ContextualLLMResult(Generic[T]):
    """Invoker 返回（不进入 State；调用方只写 value / context_state_ref）。"""

    value: T
    snapshot_public_id: str
    context_state_ref: Any = None
    token_usage: dict[str, int] | None = None
    provider_request_id: str | None = None
    attempts: tuple[LLMAttemptRef, ...] = ()


class ContextAwareLLMInvoker:
    """统一 LLM 调用：compose → sent → provider → complete → parse。"""

    def __init__(
        self,
        *,
        engine,
        snapshot_writer,
        llm_client_factory,
        parser_factory=None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._engine = engine
        self._snapshot_writer = snapshot_writer
        self._llm_client_factory = llm_client_factory
        self._parser_factory = parser_factory
        self._retry_policy = retry_policy or RetryPolicy()

    async def invoke(
        self,
        *,
        request: ContextRequest,
        llm_task_profile,
        runtime_context,
        retry_policy: RetryPolicy | None = None,
    ) -> ContextualLLMResult:
        policy = retry_policy or self._retry_policy
        attempts: list[LLMAttemptRef] = []
        started = _utcnow()

        # ── 取消检查（begin 前）：无 Snapshot + 安全 Trace，不调 LLM ──
        if _is_cancelled(runtime_context, request.task_id):
            raise_engine_error(
                code="context.invoker.cancelled_before_begin",
                detail="begin 前已取消（无 Snapshot，未调 LLM）",
                stage=ContextEngineStage.SNAPSHOT,
                retryable=False,
                recoverable=True,
            )

        # ── 首次 compose（begin + ready）──
        composed = await self._engine.compose(
            request, runtime_context=runtime_context, execution_mode="active"
        )
        snapshot_public_id = composed.snapshot_public_id
        if not snapshot_public_id:
            raise_engine_error(
                code="context.invoker.no_snapshot",
                detail="compose 未产生 Snapshot",
                stage=ContextEngineStage.SNAPSHOT,
                retryable=False,
                recoverable=True,
            )

        attempt = 1
        attempts.append(
            LLMAttemptRef(
                attempt=attempt,
                snapshot_public_id=snapshot_public_id,
                started_at=started,
                kind="initial",
            )
        )

        # ── sent 前取消检查 ──
        if _is_cancelled(runtime_context, request.task_id):
            await self._abandon(runtime_context, snapshot_public_id, request)
            raise_engine_error(
                code="context.invoker.cancelled_before_sent",
                detail="sent 前已取消（Snapshot abandoned）",
                stage=ContextEngineStage.SNAPSHOT,
                retryable=False,
                recoverable=True,
            )

        llm_client = self._llm_client_factory(runtime_context)

        # ── Provider 调用 ──
        await self._mark_sent(runtime_context, snapshot_public_id, request)
        provider_kwargs = {
            "system_prompt_override": _effective_system_prompt(llm_task_profile, composed),
        }
        if request.image_paths:
            provider_kwargs["images"] = request.image_paths
        result = await llm_client.generate_with_profile(
            llm_task_profile, composed.prompt_text or "", **provider_kwargs
        )

        if not result.success:
            # Provider 失败 / parse 失败（无 fallback）
            # WP-BE-09 fix: 但若 ``parsed`` 已设置（FALLBACK_DEFAULT 应用了 profile.fallback_text），
            # 不应当作真失败 —— 直接消费 fallback 值。
            # 否则普通 Chat / Intent 收到 LLM 返回非 JSON 时永远降级为 CLARIFY，
            # 而非 ``profile.fallback_text`` 派生的语义降级（unknown/clarify）。
            parsed_fallback = getattr(result, "parsed", None)
            if parsed_fallback is not None:
                token_usage = extract_usage(result)
                await self._complete(
                    runtime_context,
                    snapshot_public_id,
                    request,
                    actual_input_tokens=token_usage.get("input") if token_usage else None,
                    actual_output_tokens=token_usage.get("output") if token_usage else None,
                    llm_latency_ms=llm_latency_ms(result),
                )
                return ContextualLLMResult(
                    value=parsed_fallback,
                    snapshot_public_id=snapshot_public_id,
                    context_state_ref=_state_ref(composed, request, snapshot_public_id),
                    token_usage=token_usage,
                    provider_request_id=provider_request_id(result),
                    attempts=tuple(attempts),
                )
            safe_error = map_provider_error(result)
            await self._fail(runtime_context, snapshot_public_id, request, safe_error)

            if safe_error.context_length_error and attempt <= policy.max_context_length_retries:
                # Context Length Retry：compose_for_retry → 新 snapshot → 重试
                previous_ref = ContextSnapshotRef(
                    public_id=snapshot_public_id, status="failed"
                )
                retried = await self._context_length_retry(
                    request,
                    llm_task_profile,
                    runtime_context,
                    previous_ref,
                    policy,
                    attempts,
                    attempt,
                )
                return retried

            if safe_error.retryable and attempt <= policy.max_provider_retries:
                # Provider Retry（网络/限流）
                attempt += 1
                return await self._provider_retry(
                    request,
                    llm_task_profile,
                    runtime_context,
                    policy,
                    attempts,
                    attempt,
                    safe_error,
                )

            return ContextualLLMResult(
                value=None,
                snapshot_public_id=snapshot_public_id,
                context_state_ref=_state_ref(composed, request, snapshot_public_id),
                provider_request_id=safe_error.provider_request_id,
                attempts=tuple(attempts),
            )

        # ── Provider 成功：complete（无论 parser 结果）──
        token_usage = extract_usage(result)
        await self._complete(
            runtime_context,
            snapshot_public_id,
            request,
            actual_input_tokens=token_usage.get("input") if token_usage else None,
            actual_output_tokens=token_usage.get("output") if token_usage else None,
            llm_latency_ms=llm_latency_ms(result),
        )

        # ── Parse ──
        try:
            value = parse_result(result, self._parser_factory)
            return ContextualLLMResult(
                value=value,
                snapshot_public_id=snapshot_public_id,
                context_state_ref=_state_ref(composed, request, snapshot_public_id),
                token_usage=token_usage,
                provider_request_id=provider_request_id(result),
                attempts=tuple(attempts),
            )
        except Exception as exc:  # noqa: BLE001 — parser 失败
            # Schema Retry：原 Snapshot 保持 completed；新 snapshot + 重试
            if attempt <= policy.max_schema_retries:
                retried = await self._schema_retry(
                    request,
                    llm_task_profile,
                    runtime_context,
                    policy,
                    attempts,
                    attempt,
                    str(exc),
                )
                return retried
            raise

    async def stream(
        self,
        *,
        request: ContextRequest,
        llm_task_profile,
        runtime_context,
        retry_policy: RetryPolicy | None = None,
    ) -> AsyncIterator[str]:
        """统一流式 LLM 调用：compose → sent → provider stream → complete。

        该入口用于普通 Chat 这类 text output contract。它保留 Context Engine 的
        prompt/snapshot 生命周期，同时把 provider 的 chunk 原样向上游传播。
        结构化 JSON 任务仍使用 ``invoke``，避免把半截 JSON 暴露给调用方。
        """
        del retry_policy  # 流式 retry 需要 provider 支持可重放，当前先保持单次流。

        if _is_cancelled(runtime_context, request.task_id):
            raise_engine_error(
                code="context.invoker.cancelled_before_begin",
                detail="begin 前已取消（无 Snapshot，未调 LLM）",
                stage=ContextEngineStage.SNAPSHOT,
                retryable=False,
                recoverable=True,
            )

        composed = await self._engine.compose(
            request, runtime_context=runtime_context, execution_mode="active"
        )
        snapshot_public_id = composed.snapshot_public_id
        if not snapshot_public_id:
            raise_engine_error(
                code="context.invoker.no_snapshot",
                detail="compose 未产生 Snapshot",
                stage=ContextEngineStage.SNAPSHOT,
                retryable=False,
                recoverable=True,
            )

        if _is_cancelled(runtime_context, request.task_id):
            await self._abandon(runtime_context, snapshot_public_id, request)
            raise_engine_error(
                code="context.invoker.cancelled_before_sent",
                detail="sent 前已取消（Snapshot abandoned）",
                stage=ContextEngineStage.SNAPSHOT,
                retryable=False,
                recoverable=True,
            )

        llm_client = self._llm_client_factory(runtime_context)
        await self._mark_sent(runtime_context, snapshot_public_id, request)
        stream_with_profile = getattr(llm_client, "stream_with_profile", None)
        if stream_with_profile is None:
            safe_error = SafeLLMError(
                code="llm.provider.stream_unavailable",
                detail="当前 LLMClient 不支持流式 profile 调用",
                retryable=False,
            )
            await self._fail(runtime_context, snapshot_public_id, request, safe_error)
            raise_engine_error(
                code="context.invoker.stream_unavailable",
                detail=safe_error.detail,
                stage=ContextEngineStage.PAYLOAD,
                retryable=False,
                recoverable=True,
            )

        start = time.monotonic()
        emitted = False
        provider_usage: dict[str, int] = {}

        def capture_usage(usage: dict[str, int]) -> None:
            provider_usage.update(usage)

        stream_kwargs = {
            "system_prompt_override": _effective_system_prompt(llm_task_profile, composed),
        }
        # Keep third-party and test adapters with the old stream signature
        # compatible; without usage metadata ContextUsage stays heuristic.
        try:
            signature = inspect.signature(stream_with_profile)
            supports_usage_callback = (
                "on_usage" in signature.parameters
                or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD
                    for param in signature.parameters.values()
                )
            )
        except (TypeError, ValueError):
            supports_usage_callback = False
        if supports_usage_callback:
            stream_kwargs["on_usage"] = capture_usage
        try:
            async for chunk in stream_with_profile(
                llm_task_profile,
                composed.prompt_text or "",
                **stream_kwargs,
            ):
                if not chunk:
                    continue
                emitted = True
                yield str(chunk)
        except Exception as exc:  # noqa: BLE001
            safe_error = SafeLLMError(
                code="llm.provider.stream_error",
                detail=f"流式 LLM 调用失败：{type(exc).__name__}",
                retryable=True,
            )
            await self._fail(runtime_context, snapshot_public_id, request, safe_error)
            raise

        latency_ms = int((time.monotonic() - start) * 1000)
        if not emitted:
            safe_error = SafeLLMError(
                code="llm.provider.empty_stream",
                detail="流式 LLM 调用未返回任何文本片段",
                retryable=True,
            )
            await self._fail(runtime_context, snapshot_public_id, request, safe_error)
            raise_engine_error(
                code="context.invoker.empty_stream",
                detail=safe_error.detail,
                stage=ContextEngineStage.PAYLOAD,
                retryable=True,
                recoverable=True,
            )

        await self._complete(
            runtime_context,
            snapshot_public_id,
            request,
            actual_input_tokens=provider_usage.get("input") if provider_usage else None,
            actual_output_tokens=provider_usage.get("output") if provider_usage else None,
            llm_latency_ms=latency_ms,
        )

    # ── Retry 分支 ───────────────────────────────────────────────────

    async def _schema_retry(
        self,
        request,
        llm_task_profile,
        runtime_context,
        policy: RetryPolicy,
        attempts,
        attempt: int,
        error_detail: str,
    ) -> ContextualLLMResult:
        """Schema Retry：新 snapshot + schema_feedback 提示词 → 重试。"""
        new_attempt = attempt + 1
        composed = await self._engine.compose(
            request, runtime_context=runtime_context, execution_mode="active"
        )
        new_snapshot_id = composed.snapshot_public_id
        attempts.append(
            LLMAttemptRef(
                attempt=new_attempt,
                snapshot_public_id=new_snapshot_id,
                error_code="context.invoker.schema_parse_failure",
                retryable=True,
                started_at=_utcnow(),
                kind="schema_retry",
            )
        )
        await self._mark_sent(runtime_context, new_snapshot_id, request)
        llm_client = self._llm_client_factory(runtime_context)
        # schema_feedback 提示词：验证错误摘要 + 输出合同提醒，不含原始堆栈
        feedback = _schema_feedback_prompt(error_detail)
        base_prompt = _effective_system_prompt(llm_task_profile, composed) or ""
        result = await llm_client.generate_with_profile(
            llm_task_profile,
            composed.prompt_text or "",
            system_prompt_override=base_prompt + "\n" + feedback,
        )
        if result.success:
            token_usage = extract_usage(result)
            await self._complete(
                runtime_context,
                new_snapshot_id,
                request,
                actual_input_tokens=token_usage.get("input") if token_usage else None,
                actual_output_tokens=token_usage.get("output") if token_usage else None,
                llm_latency_ms=llm_latency_ms(result),
            )
            value = parse_result(result, self._parser_factory)
            return ContextualLLMResult(
                value=value,
                snapshot_public_id=new_snapshot_id,
                context_state_ref=_state_ref(composed, request, new_snapshot_id),
                token_usage=token_usage,
                provider_request_id=provider_request_id(result),
                attempts=tuple(attempts),
            )
        safe_error = map_provider_error(result)
        await self._fail(runtime_context, new_snapshot_id, request, safe_error)
        return ContextualLLMResult(
            value=None,
            snapshot_public_id=new_snapshot_id,
            context_state_ref=_state_ref(composed, request, new_snapshot_id),
            provider_request_id=safe_error.provider_request_id,
            attempts=tuple(attempts),
        )

    async def _provider_retry(
        self,
        request,
        llm_task_profile,
        runtime_context,
        policy: RetryPolicy,
        attempts,
        attempt: int,
        previous_error: SafeLLMError,
    ) -> ContextualLLMResult:
        """Provider Retry：重试原 prompt（网络/限流）。"""
        composed = await self._engine.compose(
            request,
            runtime_context=runtime_context,
            execution_mode="active",
        )
        snapshot_public_id = composed.snapshot_public_id
        if not snapshot_public_id:
            raise_engine_error(
                code="context.invoker.no_snapshot",
                detail="provider retry compose did not produce a Snapshot",
                stage=ContextEngineStage.SNAPSHOT,
                retryable=False,
                recoverable=True,
            )
        attempts.append(
            LLMAttemptRef(
                attempt=attempt,
                snapshot_public_id=snapshot_public_id,
                error_code=previous_error.code,
                retryable=True,
                started_at=_utcnow(),
                kind="provider_retry",
            )
        )
        await self._mark_sent(runtime_context, snapshot_public_id, request)
        llm_client = self._llm_client_factory(runtime_context)
        provider_kwargs = {
            "system_prompt_override": _effective_system_prompt(llm_task_profile, composed),
        }
        if request.image_paths:
            provider_kwargs["images"] = request.image_paths
        result = await llm_client.generate_with_profile(
            llm_task_profile, composed.prompt_text or "", **provider_kwargs
        )
        if result.success:
            token_usage = extract_usage(result)
            await self._complete(
                runtime_context,
                composed.snapshot_public_id,
                request,
                actual_input_tokens=token_usage.get("input") if token_usage else None,
                actual_output_tokens=token_usage.get("output") if token_usage else None,
                llm_latency_ms=llm_latency_ms(result),
            )
            value = parse_result(result, self._parser_factory)
            return ContextualLLMResult(
                value=value,
                snapshot_public_id=composed.snapshot_public_id,
                context_state_ref=_state_ref(composed, request, composed.snapshot_public_id),
                token_usage=token_usage,
                provider_request_id=provider_request_id(result),
                attempts=tuple(attempts),
            )
        safe_error = map_provider_error(result)
        await self._fail(runtime_context, composed.snapshot_public_id, request, safe_error)
        return ContextualLLMResult(
            value=None,
            snapshot_public_id=composed.snapshot_public_id,
            context_state_ref=_state_ref(composed, request, composed.snapshot_public_id),
            provider_request_id=safe_error.provider_request_id,
            attempts=tuple(attempts),
        )

    async def _context_length_retry(
        self,
        request,
        llm_task_profile,
        runtime_context,
        previous_ref: ContextSnapshotRef,
        policy: RetryPolicy,
        attempts,
        attempt: int,
    ) -> ContextualLLMResult:
        """Context Length Retry：compose_for_retry（确定性降容）→ 新 snapshot。"""
        new_attempt = attempt + 1
        composed = await self._engine.compose_for_retry(
            request, runtime_context=runtime_context, previous_snapshot_ref=previous_ref
        )
        new_snapshot_id = composed.snapshot_public_id
        attempts.append(
            LLMAttemptRef(
                attempt=new_attempt,
                snapshot_public_id=new_snapshot_id,
                error_code="context_length_error",
                retryable=True,
                started_at=_utcnow(),
                kind="context_length_retry",
            )
        )
        await self._mark_sent(runtime_context, new_snapshot_id, request)
        llm_client = self._llm_client_factory(runtime_context)
        provider_kwargs = {
            "system_prompt_override": _effective_system_prompt(llm_task_profile, composed),
        }
        if request.image_paths:
            provider_kwargs["images"] = request.image_paths
        result = await llm_client.generate_with_profile(
            llm_task_profile, composed.prompt_text or "", **provider_kwargs
        )
        if result.success:
            token_usage = extract_usage(result)
            await self._complete(
                runtime_context,
                new_snapshot_id,
                request,
                actual_input_tokens=token_usage.get("input") if token_usage else None,
                actual_output_tokens=token_usage.get("output") if token_usage else None,
                llm_latency_ms=llm_latency_ms(result),
            )
            value = parse_result(result, self._parser_factory)
            return ContextualLLMResult(
                value=value,
                snapshot_public_id=new_snapshot_id,
                context_state_ref=_state_ref(composed, request, new_snapshot_id),
                token_usage=token_usage,
                provider_request_id=provider_request_id(result),
                attempts=tuple(attempts),
            )
        safe_error = map_provider_error(result)
        await self._fail(runtime_context, new_snapshot_id, request, safe_error)
        return ContextualLLMResult(
            value=None,
            snapshot_public_id=new_snapshot_id,
            context_state_ref=_state_ref(composed, request, new_snapshot_id),
            provider_request_id=safe_error.provider_request_id,
            attempts=tuple(attempts),
        )

    # ── Snapshot 写操作 ───────────────────────────────────────────────

    async def _mark_sent(self, runtime_context, public_id: str, request) -> None:
        async with runtime_context.session_factory() as session:
            await self._snapshot_writer.mark_sent(
                session=session, public_id=public_id, user_id=_user_internal_id(runtime_context, request)
            )
            await session.commit()

    async def _complete(
        self,
        runtime_context,
        public_id: str,
        request,
        *,
        actual_input_tokens,
        actual_output_tokens,
        llm_latency_ms,
    ) -> None:
        async with runtime_context.session_factory() as session:
            await self._snapshot_writer.complete(
                ContextSnapshotCompleteCommand(
                    snapshot_public_id=public_id,
                    actual_input_tokens=actual_input_tokens,
                    actual_output_tokens=actual_output_tokens,
                    llm_latency_ms=llm_latency_ms,
                    completed_at=_utcnow(),
                ),
                session=session,
                user_id=_user_internal_id(runtime_context, request),
            )
            await session.commit()

    async def _fail(
        self, runtime_context, public_id: str, request, safe_error: SafeLLMError
    ) -> None:
        async with runtime_context.session_factory() as session:
            await self._snapshot_writer.fail(
                ContextSnapshotFailCommand(
                    snapshot_public_id=public_id,
                    error_code=safe_error.code,
                    detail=safe_error.detail,
                    failed_at=_utcnow(),
                ),
                session=session,
                user_id=_user_internal_id(runtime_context, request),
            )
            await session.commit()

    async def _abandon(self, runtime_context, public_id: str, request) -> None:
        async with runtime_context.session_factory() as session:
            await self._snapshot_writer.abandon(
                session=session, public_id=public_id, user_id=_user_internal_id(runtime_context, request)
            )
            await session.commit()


# ── 辅助 ────────────────────────────────────────────────────────────

def _state_ref(composed, request, snapshot_public_id: str | None):
    """Build the authoritative lightweight state reference from composed data."""
    return build_context_state_ref(
        composed,
        snapshot_public_id=snapshot_public_id,
        profile_key=request.call_site,
    )


def _user_internal_id(runtime_context, request) -> int:
    """snapshot.user_id 使用内部 id（RuntimeContext.user_internal_id）。"""
    internal = getattr(runtime_context, "user_internal_id", None)
    if internal is not None:
        return int(internal)
    return int(request.user_id)


def _is_cancelled(runtime_context, task_id: str | None) -> bool:
    cancellation_service = getattr(runtime_context, "cancellation_service", None)
    if cancellation_service is None or task_id is None:
        return False
    try:
        return bool(cancellation_service.is_cancelled(task_id))
    except Exception:  # noqa: BLE001
        return False


def _extract_system_prompt(composed) -> str | None:
    """从 ComposeResult 提取 system prompt（多条 system 消息拼接）。"""
    system_parts = [m.content for m in composed.messages if m.role == "system"]
    if not system_parts:
        return None
    return "\n\n".join(system_parts)


def _effective_system_prompt(llm_task_profile, composed) -> str | None:
    """Keep the task profile contract while adding composed Context Engine rules."""
    parts = [str(getattr(llm_task_profile, "system_prompt", "") or "").strip()]
    parts.append(str(_extract_system_prompt(composed) or "").strip())
    merged = [part for part in parts if part]
    return "\n\n".join(merged) if merged else None


def _schema_feedback_prompt(error_detail: str) -> str:
    """Schema Retry 提示词：验证错误摘要 + 输出合同提醒，不含原始堆栈。"""
    detail = (error_detail or "")[:500]
    return (
        "你的上一次输出未通过 schema 校验。请按输出合同重新输出。\n"
        f"校验摘要：{detail}"
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
