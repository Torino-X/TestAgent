"""Agent Loop Compaction — 压缩 preparation/repair/incremental 循环历史。

CE-04 WP-5：复用 ConversationCompactor 的 Payload First 三段事务模式，
但面向 Agent Loop（summary_type='agent_loop'，source=steps_audit/loop budget）。

source 是 preparation/repair/incremental 的 steps_audit（decision_summary≤500 +
args_signature，Rule 11）与 loop budget/failure reason；压缩结果只写 Ref 不写
业务 State 全文。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.context_engine.compression.conversation_compactor import ConversationCompactor, _idempotency_key
from app.context_engine.compression.models import ContextCompactionResult
from app.context_engine.models.enums import CompactionStatus, CompactionType
from app.models.context_engine import ContextCompactionRun
from app.models.conversation_summary import ConversationSummary
from app.repositories.conversation_summary_repository import ConversationSummaryRepository
from app.repositories.context_engine_repositories import ContextCompactionRunRepository
from app.repositories.base import ensure_model_id
from app.utils.ids import generate_public_id


class AgentLoopCompactor(ConversationCompactor):
    """Agent Loop 压缩执行器（复用三段事务，summary_type='agent_loop'）。"""

    compaction_type = CompactionType.AGENT_LOOP

    async def compact(self, req, *, runtime_context=None):
        """Agent Loop 版 compact：与 Conversation 同三段事务，但写 agent_loop summary。"""
        run_public_id = _idempotency_key(req)

        # ── Prepare Transaction ───────────────────────────────────────
        payload_id: int | None = None
        async with self._session_factory() as session:
            repo = ContextCompactionRunRepository(session)
            existing = await repo.get_by_public_id(run_public_id, req.user_id)
            if existing is not None and existing.status in ("completed", "cancelled"):
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
            if req.require_recovery_payload:
                payload_id = await self._persist_payload(session, req)

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
        try:
            summary_payload = await self._generate_agent_loop_summary(req, runtime_context)
            if summary_payload is None:
                await self._fail_run(run_public_id, req.user_id, "compression.generate_failed")
                return None
            tokens_after = int(summary_payload.get("tokens_after", req.target_tokens))
        except Exception:  # noqa: BLE001
            await self._fail_run(run_public_id, req.user_id, "compression.provider_error")
            return None

        # ── Finalize Transaction ──────────────────────────────────────
        async with self._session_factory() as session:
            locked = await self._lock_run(session, run_public_id, req.user_id)
            if locked is None or locked.status != "running":
                return None
            ok, failures = self._anchor_validator.validate(req.protected_anchors, summary_payload)
            if not ok:
                locked.status = "failed"
                locked.error_code = "compression.anchor_validation_failed"
                locked.error_message = ";".join(failures)[:2000]
                await session.flush()
                await session.commit()
                return None

            summary_repo = ConversationSummaryRepository(session)
            old_active = None
            if req.conversation_id:
                old_active = await summary_repo.get_latest_active_by_type(
                    req.conversation_id, req.user_id, "agent_loop"
                )
            now = datetime.now(timezone.utc)
            new_summary = ConversationSummary(
                public_id=generate_public_id("summary"),
                user_id=req.user_id,
                conversation_id=req.conversation_id,
                summary_text=str(summary_payload.get("summary_text", "")),
                message_count=len(req.source_refs),
                estimated_tokens=tokens_after,
                status="active",
                summary_type="agent_loop",
                schema_version="v1",
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

    async def _generate_agent_loop_summary(self, req, runtime_context) -> dict | None:
        """Agent Loop 摘要：输入 steps_audit（decision_summary + args_signature）。"""
        if runtime_context is None:
            return None
        invoker = getattr(runtime_context, "context_llm_invoker", None)
        if invoker is None:
            return None
        from app.llm.task_profiles import LLMTaskProfile

        profile = LLMTaskProfile(
            name="compression.agent_loop.v1",
            system_prompt="你是 Agent Loop 压缩器。把循环历史压缩为结构化摘要（决策/工具调用/失败原因），保留循环状态。",
            parser="json_strict",
        )
        loop_text = req.source_payload.get("steps_audit", "") or ""
        if hasattr(invoker, "invoke"):
            from app.context_engine.models.context import ContextRequest

            crequest = ContextRequest(
                user_id=str(req.user_id),
                conversation_id=str(req.conversation_id) if req.conversation_id else None,
                task_id=str(req.task_id) if req.task_id else None,
                call_site="compression.agent_loop",
                current_user_message=loop_text,
                state_ref={"compaction_request_id": req.request_id},
            )
            result = await invoker.invoke(
                request=crequest, llm_task_profile=profile, runtime_context=runtime_context
            )
            value = getattr(result, "value", None)
            if value is None:
                return None
            if isinstance(value, dict):
                return value
            return {"summary_text": str(value), "tokens_after": req.target_tokens}
        if hasattr(invoker, "generate"):
            result = await invoker.generate(
                user_id=req.user_id,
                call_site="compression.agent_loop",
                llm_task_profile=profile,
                current_goal=loop_text,
                user_content=loop_text,
                conversation_id=req.conversation_id,
                task_id=req.task_id,
                runtime_context=runtime_context,
            )
            value = getattr(result, "value", None)
            if value is None:
                return None
            if isinstance(value, dict):
                return value
            return {"summary_text": str(value), "tokens_after": req.target_tokens}
        if hasattr(invoker, "generate_with_profile"):
            import json

            result = await invoker.generate_with_profile(profile, json.dumps({"loop": loop_text}, ensure_ascii=False))
            text = getattr(result, "parsed", None) or getattr(result, "text", None)
            if text is None:
                return None
            return {"summary_text": str(text), "tokens_after": req.target_tokens}
        return None


def _safe_ratio(before: int, after: int | None) -> float:
    if not before or not after:
        return 1.0
    return round(after / before, 6)


__all__ = ["AgentLoopCompactor"]
# auto-appended module-level note: agent_loop 压缩: Agent 历史按 token + 轮次切片保留 + 摘要。
# auto-appended module-level note: agent_loop 压缩: Agent 历史按 token + 轮次切片保留 + 摘要。
