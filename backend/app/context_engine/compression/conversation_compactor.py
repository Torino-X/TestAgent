"""Conversation Compaction — Payload First 三段事务 + 版本链。

The compaction provider call intentionally bypasses ordinary Context Engine
composition: its payload is the over-limit context that compaction is meant to
repair, so a second preflight/compose cycle can reject that payload before a
summary is generated. The compaction run remains the authoritative audit
record. Three-phase transactions (DB transactions never cross the LLM call):
  Prepare  事务内创建 compaction run + 持久化 recovery payload + COMMIT
  Provider 事务外经 Invoker 生成摘要 + Anchor/Schema/Token 校验
  Finalize 事务内 FOR UPDATE 锁 run + CAS + 新 summary active + 旧 superseded
           + run completed + COMMIT
失败：run→failed；旧 active summary 保持；payload→recoverable/orphan_pending_gc。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.context_engine.compression.anchor import AnchorValidator
from app.context_engine.compression.models import ContextCompactionRequest, ContextCompactionResult
from app.context_engine.models.context import ContextRequest
from app.context_engine.models.enums import CompactionStatus, CompactionType, RecoveryMode
from app.context_engine.conversation_retention import summary_schema_version_for_policy
from app.llm.task_profiles import LLMTaskProfile
from app.models.context_engine import ContextCompactionRun, ContextPayload
from app.models.conversation_summary import ConversationSummary
from app.repositories.conversation_summary_repository import ConversationSummaryRepository
from app.repositories.context_engine_repositories import ContextCompactionRunRepository
from app.repositories.base import ensure_model_id
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)

_DEFAULT_TTL_DAYS = 7


class _CompactionGenerationFailure(RuntimeError):
    """Safe provider-result failure used for bounded retry telemetry."""

    def __init__(self, reason: str) -> None:
        safe = "".join(ch for ch in str(reason or "unknown") if ch.isalnum() or ch in "._-")
        self.safe_reason = (safe or "unknown")[:64]
        super().__init__(self.safe_reason)


def _idempotency_key(req: ContextCompactionRequest) -> str:
    """CompactionRun idempotency key（→ run.public_id）。"""
    scope = req.conversation_id or req.task_id or ""
    raw = f"{req.call_site}|{req.user_id}|{scope}|{req.trigger.value}|{req.tokens_before}|{req.source_digest}"
    return "compaction_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class ConversationCompactor:
    """Conversation 压缩执行器（Payload First 三段事务）。"""

    compaction_type = CompactionType.CONVERSATION

    def __init__(
        self,
        *,
        anchor_validator: AnchorValidator | None = None,
        session_factory=None,
        payload_store=None,
        max_attempts: int = 2,
    ) -> None:
        self._anchor_validator = anchor_validator or AnchorValidator()
        self._session_factory = session_factory
        self._payload_store = payload_store
        self._max_attempts = max(1, int(max_attempts))

    async def compact(
        self,
        req: ContextCompactionRequest,
        *,
        runtime_context=None,
    ) -> ContextCompactionResult | None:
        run_public_id = _idempotency_key(req)

        # ── Prepare Transaction ───────────────────────────────────────
        payload_id: int | None = None
        async with self._session_factory() as session:
            repo = ContextCompactionRunRepository(session)
            existing = await repo.get_by_public_id(run_public_id, req.user_id)
            if existing is not None and existing.status in ("completed", "cancelled"):
                # 幂等：同 idempotency key 已完成 → 返回既有结果
                summary_text, summary_type = await self._load_summary_payload(
                    existing.output_summary_public_id, req.user_id
                )
                return ContextCompactionResult(
                    run_public_id=run_public_id,
                    summary_public_id=existing.output_summary_public_id,
                    summary_text=summary_text,
                    summary_type=summary_type,
                    recovery_payload_id=existing.recovery_payload_id,
                    tokens_after=existing.tokens_after or 0,
                    compression_ratio=_safe_ratio(req.tokens_before, existing.tokens_after),
                    status=CompactionStatus.COMPACTED,
                )

            # 需要恢复能力 → 先持久化 recovery payload（Payload First）
            if req.require_recovery_payload:
                payload_id = await self._persist_payload(session, req)

            # 创建/复用 compaction run
            run = existing
            if run is None:
                run = ContextCompactionRun(
                    public_id=run_public_id,
                    user_id=req.user_id,
                    workspace_key=req.workspace_key,
                    conversation_id=req.conversation_id,
                    task_id=req.task_id,
                    call_site=req.call_site,
                    compaction_type=req.compaction_type.value,
                    trigger_type=req.trigger.value,
                    policy_key=req.policy_key,
                    policy_version=req.policy_version,
                    tokens_before=req.tokens_before,
                    target_tokens=req.target_tokens,
                    protected_anchors_json=[a.to_state_dict() for a in req.protected_anchors],
                    source_refs_json=[r.to_state_dict() for r in req.source_refs],
                    recovery_mode=req.recovery_mode.value,
                    status="running",
                    attempt=1,
                    recovery_payload_id=payload_id,
                    created_at=datetime.now(timezone.utc),
                )
                await ensure_model_id(session, ContextCompactionRun, run)
                session.add(run)
                await session.flush()
            else:
                run.attempt = (run.attempt or 0) + 1
                run.status = "running"
                run.recovery_payload_id = payload_id or run.recovery_payload_id
                await session.flush()
            await session.commit()

        # ── Provider Phase（事务外）──────────────────────────────────
        summary_payload: dict[str, Any] | None = None
        failure_code = "compression.generate_failed"
        failure_message: str | None = None
        for provider_attempt in range(1, self._max_attempts + 1):
            try:
                summary_payload = await self._generate_summary(req, runtime_context)
                if summary_payload is None:
                    raise _CompactionGenerationFailure("empty_result")
                break
            except _CompactionGenerationFailure as exc:
                failure_code = "compression.generate_failed"
                failure_message = f"provider_result:{exc.safe_reason}"
                logger.warning(
                    "compaction generation failed | run=%s | attempt=%d/%d | reason=%s",
                    run_public_id,
                    provider_attempt,
                    self._max_attempts,
                    exc.safe_reason,
                )
            except Exception as exc:  # noqa: BLE001
                failure_code = "compression.provider_error"
                failure_message = f"provider_exception:{type(exc).__name__}"
                logger.warning(
                    "compaction provider failed | run=%s | attempt=%d/%d | error=%s",
                    run_public_id,
                    provider_attempt,
                    self._max_attempts,
                    type(exc).__name__,
                )

        if summary_payload is None:
            await self._fail_run(
                run_public_id,
                req.user_id,
                failure_code,
                error_message=failure_message,
            )
            return None
        tokens_after = int(summary_payload.get("tokens_after", req.target_tokens))

        # ── Finalize Transaction ──────────────────────────────────────
        async with self._session_factory() as session:
            # FOR UPDATE 锁 run 行 + CAS
            locked = await self._lock_run(session, run_public_id, req.user_id)
            if locked is None or locked.status != "running":
                # 旧 Worker 延迟提交拒绝
                logger.warning("compaction finalize rejected | run=%s | status=%s", run_public_id, getattr(locked, "status", None))
                return None
            if locked.attempt != self._expected_attempt(req, locked):
                return None

            # Anchor 服务端校验
            ok, failures = self._anchor_validator.validate(req.protected_anchors, summary_payload)
            if not ok:
                locked.status = "failed"
                locked.error_code = "compression.anchor_validation_failed"
                locked.error_message = ";".join(failures)[:2000]
                await session.flush()
                await session.commit()
                return None

            # 版本链：新 summary active + 旧 active → superseded
            summary_repo = ConversationSummaryRepository(session)
            old_active = None
            if req.conversation_id:
                old_active = await summary_repo.get_latest_active_by_type(
                    req.conversation_id, req.user_id, "conversation"
                )
            now = datetime.now(timezone.utc)
            new_summary = ConversationSummary(
                public_id=generate_public_id("summary"),
                user_id=req.user_id,
                conversation_id=req.conversation_id,
                summary_text=str(summary_payload.get("summary_text", "")),
                covered_message_start_id=req.covered_message_start_id,
                covered_message_end_id=req.covered_message_end_id,
                message_count=(
                    req.covered_message_count
                    if req.covered_message_count is not None
                    else len(req.source_refs)
                ),
                estimated_tokens=tokens_after,
                summary_version=(
                    (getattr(old_active, "summary_version", 0) or 0) + 1
                ),
                status="active",
                summary_type="conversation",
                # Keep the selected raw-tail contract with the durable
                # summary.  Source and ledger reconstruction read this value
                # after refresh/restart; legacy summaries continue as v1.
                schema_version=summary_schema_version_for_policy(req.policy_key),
                protected_anchors_json=[a.to_state_dict() for a in req.protected_anchors],
                source_refs_json=[r.to_state_dict() for r in req.source_refs],
                recovery_mode=req.recovery_mode.value,
                recovery_payload_id=payload_id,
                tokens_before=req.tokens_before,
                tokens_after=tokens_after,
                compression_ratio=_safe_ratio(req.tokens_before, tokens_after),
                source_digest=req.source_digest,
                created_at=now,
                updated_at=now,
            )
            await ensure_model_id(session, ConversationSummary, new_summary)
            session.add(new_summary)
            await session.flush()
            if old_active is not None:
                await summary_repo.supersede(old_active.id, req.user_id, new_summary.id)

            locked.status = "completed"
            locked.output_summary_public_id = new_summary.public_id
            locked.tokens_after = tokens_after
            locked.compression_ratio = _safe_ratio(req.tokens_before, tokens_after)
            locked.completed_at = now
            await session.flush()
            await session.commit()

            return ContextCompactionResult(
                run_public_id=run_public_id,
                summary_public_id=new_summary.public_id,
                summary_text=new_summary.summary_text,
                summary_type=new_summary.summary_type,
                recovery_payload_id=payload_id,
                tokens_after=tokens_after,
                compression_ratio=_safe_ratio(req.tokens_before, tokens_after),
                status=CompactionStatus.COMPACTED,
            )

    # ── Provider 摘要（经 Invoker）───────────────────────────────────

    async def _generate_summary(self, req: ContextCompactionRequest, runtime_context) -> dict[str, Any] | None:
        """经 context_llm_invoker 生成压缩摘要（事务外）。"""
        if runtime_context is None:
            raise _CompactionGenerationFailure("runtime_context_unavailable")
        # The selected payload may already exceed normal Hard/Absolute
        # thresholds. Calling ContextAwareLLMInvoker.invoke() would compose the
        # same payload again and can fail before the compression-model request.
        # This special provider call remains fully audited by the surrounding
        # ContextCompactionRun and uses the request-scoped configured client.
        llm_client = getattr(runtime_context, "llm_client", None)
        if llm_client is not None and hasattr(llm_client, "generate_with_profile"):
            profile = LLMTaskProfile(
                name="compression.conversation.v1",
                system_prompt=(
                    "你是 TestAgent 的上下文压缩器。把输入中的旧对话压缩为可长期续接的结构化摘要。"
                    "必须保留：用户目标与明确约束；已确认决策及理由；范围包含项与排除项；"
                    "人名、负责人、数字、日期、状态码和其他精确标识；文件、工具结果与任务状态；"
                    "未决问题、承诺和下一步。新信息明确推翻旧信息时记录替代关系；不确定内容必须标注，"
                    "不得补写输入中不存在的事实。只输出 JSON 对象："
                    '{"summary_text":"...","tokens_after":<整数>}。'
                    " Status-safety rule: never write confirmed, approved, accepted, fixed, "
                    "or established for any decision, scope, threshold, owner, or release state "
                    "unless the input explicitly states that exact status. A requirement, proposed "
                    "scope, or evidence record is not a decision. Preserve unresolved items as "
                    "unresolved; do not infer approval from the existence of evidence."
                ),
                parser="json_strict",
            )
            conversation_text = json.dumps(
                {"conversation": req.source_payload.get("conversation", "")},
                ensure_ascii=False,
            )
            result = await llm_client.generate_with_profile(profile, conversation_text)
            if not getattr(result, "success", False):
                raise _CompactionGenerationFailure(
                    f"profile_{getattr(result, 'error_type', None) or 'unsuccessful'}"
                )
            value = getattr(result, "parsed", None)
            if value is None:
                value = getattr(result, "text", None) or getattr(result, "raw_text", None)
            if value is None:
                raise _CompactionGenerationFailure("profile_empty_value")
            if isinstance(value, dict):
                return value
            return {"summary_text": str(value), "tokens_after": req.target_tokens}

        # Compatibility fallback for isolated tests and legacy runtime
        # contexts without a request-scoped client. Production normal chat
        # always supplies ``llm_client`` above.
        invoker = getattr(runtime_context, "context_llm_invoker", None)
        if invoker is None:
            raise _CompactionGenerationFailure("invoker_unavailable")
        profile = LLMTaskProfile(
            name="compression.conversation.v1",
            system_prompt=(
                "你是 TestAgent 的上下文压缩器。将旧对话压缩为可长期续接的结构化摘要，"
                "保留目标、约束、决策与理由、范围包含/排除项、负责人、数字、日期、精确标识、"
                "文件和工具结果、任务状态、未决问题与下一步；记录明确替代关系，标注不确定性，"
                "不得补写事实。"
            ),
            parser="json_strict",
        )
        conversation_text = json.dumps(
            {"conversation": req.source_payload.get("conversation", "")},
            ensure_ascii=False,
        )
        # 用 fake/provider 统一协议：优先 invoke()（ContextAwareLLMInvoker），否则 generate_with_profile
        if hasattr(invoker, "invoke"):
            crequest = ContextRequest(
                user_id=str(req.user_id),
                conversation_id=str(req.conversation_id) if req.conversation_id else None,
                task_id=str(req.task_id) if req.task_id else None,
                call_site="compression.conversation",
                current_user_message=conversation_text,
                state_ref={"compaction_request_id": req.request_id},
            )
            result = await invoker.invoke(
                request=crequest,
                llm_task_profile=profile,
                runtime_context=runtime_context,
            )
            value = getattr(result, "value", None)
            if value is None:
                raise _CompactionGenerationFailure("invoke_empty_value")
            if isinstance(value, dict):
                return value
            return {"summary_text": str(value), "tokens_after": req.target_tokens}
        if hasattr(invoker, "generate"):
            result = await invoker.generate(
                user_id=req.user_id,
                call_site="compression.conversation",
                llm_task_profile=profile,
                current_goal=conversation_text,
                user_content=conversation_text,
                conversation_id=req.conversation_id,
                task_id=req.task_id,
                runtime_context=runtime_context,
            )
            value = getattr(result, "value", None)
            if value is None:
                raise _CompactionGenerationFailure("generate_empty_value")
            if isinstance(value, dict):
                return value
            return {"summary_text": str(value), "tokens_after": req.target_tokens}
        if hasattr(invoker, "generate_with_profile"):
            result = await invoker.generate_with_profile(
                profile, conversation_text
            )
            text = getattr(result, "parsed", None) or getattr(result, "text", None)
            if text is None:
                raise _CompactionGenerationFailure("profile_empty_value")
            return {"summary_text": str(text), "tokens_after": req.target_tokens}
        raise _CompactionGenerationFailure("provider_protocol_unavailable")

    async def _load_summary_payload(self, summary_public_id: str | None, user_id: int) -> tuple[str | None, str | None]:
        if not summary_public_id:
            return None, None
        async with self._session_factory() as session:
            repo = ConversationSummaryRepository(session)
            summary = await repo.get_by_public_id(summary_public_id, user_id)
            if summary is None:
                return None, None
            return summary.summary_text, summary.summary_type

    # ── Payload 持久化（Prepare 事务内）──────────────────────────────

    async def _persist_payload(self, session, req: ContextCompactionRequest) -> int:
        """持久化 recovery payload/manifest 到 context_payloads，返回 id。"""
        data = json.dumps(
            {
                "source_refs": [r.to_state_dict() for r in req.source_refs],
                "source_payload": req.source_payload,
                "manifest_schema": "recovery_manifest_v1",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        content = data
        storage_key = generate_public_id("pl") + ".json"
        payload = ContextPayload(
            public_id=generate_public_id("payload"),
            user_id=req.user_id,
            workspace_key=req.workspace_key,
            conversation_id=req.conversation_id,
            task_id=req.task_id,
            source_type="context_compaction",
            source_public_id=req.request_id,
            payload_type="recovery_manifest",
            storage_backend="inline",
            storage_key=storage_key,
            storage_key_hash=hashlib.sha256(storage_key.encode("utf-8")).hexdigest(),
            size_bytes=len(content),
            char_count=len(data),
            estimated_tokens=max(1, len(data) // 3),
            sha256=hashlib.sha256(content).hexdigest(),
            encrypted=False,
            status="active",
            metadata_json={"compaction_run": req.request_id},
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=_DEFAULT_TTL_DAYS),
        )
        await ensure_model_id(session, ContextPayload, payload)
        session.add(payload)
        await session.flush()
        return payload.id

    # ── Run 状态维护 ──────────────────────────────────────────────────

    async def _lock_run(self, session, run_public_id: str, user_id: int):
        """FOR UPDATE 锁定 run 行（并发锁载体）。"""
        result = await session.execute(
            select(ContextCompactionRun)
            .where(
                ContextCompactionRun.public_id == run_public_id,
                ContextCompactionRun.user_id == user_id,
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def _fail_run(
        self,
        run_public_id: str,
        user_id: int,
        error_code: str,
        *,
        error_message: str | None = None,
    ) -> None:
        try:
            async with self._session_factory() as session:
                run = await ContextCompactionRunRepository(session).get_by_public_id(run_public_id, user_id)
                if run is not None and run.status == "running":
                    run.status = "failed"
                    run.error_code = error_code
                    run.error_message = error_message
                    await session.flush()
                    await session.commit()
        except Exception:  # noqa: BLE001
            logger.warning("compaction _fail_run 失败 | run=%s", run_public_id)

    @staticmethod
    def _expected_attempt(req: ContextCompactionRequest, run) -> int:
        """期望 attempt：创建时 1，重试时递增。"""
        return getattr(run, "attempt", 1)


def _safe_ratio(before: int, after: int | None) -> float:
    """compression_ratio = tokens_after / tokens_before（越小越好）。"""
    if not before or not after:
        return 1.0
    return round(after / before, 6)


__all__ = ["ConversationCompactor", "_idempotency_key", "_safe_ratio"]
# auto-appended module-level note: 会话压缩: ConversationContextService 触发的轮次压缩主入口。
# auto-appended module-level note: 会话压缩: ConversationContextService 触发的轮次压缩主入口。
