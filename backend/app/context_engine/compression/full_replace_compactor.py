"""Full Replace — Provider Context Length 紧急重组（默认关，doc09 §26）。

Full Replace 参数：default=false、maximum_attempts<=2、profile 显式允许、
recovery payload required、anchor=100%、失败即 BLOCKED。

复用 Conversation 三段事务，但：
- summary_type='full_replace'；
- 强制 require_recovery_payload=True；
- 输出结构：Conversation Summary + Agent Loop Summary + Evidence Manifest +
  Recovery Manifest + Preserved Direct Items（source 原始来源）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.context_engine.compression.conversation_compactor import ConversationCompactor, _idempotency_key
from app.context_engine.compression.models import ContextCompactionResult
from app.context_engine.models.enums import CompactionStatus, CompactionType
from app.models.conversation_summary import ConversationSummary
from app.repositories.conversation_summary_repository import ConversationSummaryRepository
from app.repositories.base import ensure_model_id
from app.utils.ids import generate_public_id


class FullReplaceCompactor(ConversationCompactor):
    """Full Replace 压缩器（默认不启用，仅 provider_context_error + flag 开）。"""

    compaction_type = CompactionType.FULL_REPLACE

    def __init__(self, *, anchor_validator=None, session_factory=None, payload_store=None, max_attempts: int = 2):
        super().__init__(
            anchor_validator=anchor_validator,
            session_factory=session_factory,
            payload_store=payload_store,
            max_attempts=max_attempts,
        )

    async def compact(self, req, *, runtime_context=None):
        # Full Replace 强制 Payload First + recovery manifest
        req = req.model_copy(update={"require_recovery_payload": True})
        run_public_id = _idempotency_key(req)

        # ── Prepare Transaction ───────────────────────────────────────
        payload_id: int | None = None
        async with self._session_factory() as session:
            from app.repositories.context_engine_repositories import ContextCompactionRunRepository
            from app.models.context_engine import ContextCompactionRun

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
                    recovery_mode="full_rehydrate",
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
            summary_payload = await self._generate_full_replace_summary(req, runtime_context)
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
                    req.conversation_id, req.user_id, "full_replace"
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
                summary_type="full_replace",
                schema_version="v1",
                protected_anchors_json=[a.to_state_dict() for a in req.protected_anchors],
                source_refs_json=[r.to_state_dict() for r in req.source_refs],
                recovery_mode="full_rehydrate",
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

    async def _generate_full_replace_summary(self, req, runtime_context) -> dict | None:
        """Full Replace 摘要：完整重组（Conversation + Agent Loop + Evidence + Manifest）。"""
        if runtime_context is None:
            return None
        invoker = getattr(runtime_context, "context_llm_invoker", None)
        if invoker is None:
            return None
        from app.llm.task_profiles import LLMTaskProfile

        profile = LLMTaskProfile(
            name="compression.full_replace.v1",
            system_prompt="你是上下文完整重组器。在 Provider Context Length 超限时，把完整上下文重组为结构化紧凑摘要，"
                          "必须保留全部 Protected Anchors、关键事实、决策与未决项，并输出 recovery manifest 引用。",
            parser="json_strict",
        )
        source_text = req.source_payload.get("conversation", "") or req.source_payload.get("steps_audit", "") or ""
        if hasattr(invoker, "invoke"):
            from app.context_engine.models.context import ContextRequest

            crequest = ContextRequest(
                user_id=str(req.user_id),
                conversation_id=str(req.conversation_id) if req.conversation_id else None,
                task_id=str(req.task_id) if req.task_id else None,
                call_site="compression.full_replace",
                current_user_message=source_text,
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
                call_site="compression.full_replace",
                llm_task_profile=profile,
                current_goal=source_text,
                user_content=source_text,
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

            result = await invoker.generate_with_profile(profile, json.dumps({"context": source_text}, ensure_ascii=False))
            text = getattr(result, "parsed", None) or getattr(result, "text", None)
            if text is None:
                return None
            return {"summary_text": str(text), "tokens_after": req.target_tokens}
        return None


def _safe_ratio(before: int, after: int | None) -> float:
    if not before or not after:
        return 1.0
    return round(after / before, 6)


__all__ = ["FullReplaceCompactor"]
# auto-appended module-level note: full_replace 压缩: 全量替换式压缩(legacy 不变量)。
