"""Incremental Agent Fallback (Phase 2.5).

字节级复用 Phase 2.4 review-regen 主体(``regenerate_sections_node``),
不重写 regen-loop。

降级路径:

1. LLM 主循环失败 / 预算耗尽 / 永久 deny → 触发 fallback;
2. fallback 调 Phase 2.1 ``TestPlanRegenTool`` + ``ResultReviewTool`` +
   ``WordExportTool`` + ``DocxFormatCheckTool``(全量 regen 流程);
3. 仍写入 ``version_no+1`` 新 artifact,记入 ``superseded_artifact_ids``;
4. 写 ``IncrementalResult.fallback_reason = "<reason>"`` —— 主图看到
   后走 prepare_export 时显式告诉用户"已降级"。

要点:
- 永不抛(Rule 14);
- 输出 dict 与 Phase 2.4 fallback 字节级一致;
- 审计链 step 输出 ``outcome="fallback"``。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Dict, List

from app.agent_runtime.incremental.artifact_chain import (
    append_superseded_to_task_context,
    create_incremental_artifact_record,
    find_idempotent_record,
    incremental_artifact_idempotency_key,
)


_logger = logging.getLogger(__name__)


async def run_legacy_incremental_fallback(
    state: Dict[str, Any],
    *,
    ctx,
    failure_reason: str,
) -> Dict[str, Any]:
    """字节级复用 Phase 2.1 ``regenerate_sections_node`` 主体。

    Args:
        state: ``TestPlanGraphState`` 字典镜像
        ctx: ``RuntimeContext``
        failure_reason: 主循环失败原因,落到 ``IncrementalResult.fallback_reason``

    Returns:
        state delta dict,符合 LangGraph node 返回契约
    """
    logger_extra = {
        "task_id": state.get("task_id"),
        "graph_run_id": state.get("graph_run_id"),
        "reason": failure_reason,
    }

    try:
        async with ctx.session_factory() as session:
            # 1. 加载 source artifact
            from app.models.artifact import Artifact
            from app.models.agent_task import AgentTask

            source_public_id = state.get("source_artifact_public_id") or ""
            source = await session.execute(
                __select_artifact_by_public_id(source_public_id),
            )
            source_artifact = source.scalar_one_or_none()
            if source_artifact is None:
                _logger.warning(
                    "incremental_fallback_source_missing public_id=%s",
                    source_public_id,
                    extra=logger_extra,
                )
                return _fail_delta(failure_reason + ":source_missing")

            agent_task = await session.get(
                AgentTask, source_artifact.task_id,
            )
            if agent_task is None:
                _logger.warning(
                    "incremental_fallback_agent_task_missing task_id=%s",
                    source_artifact.task_id,
                    extra=logger_extra,
                )
                return _fail_delta(failure_reason + ":agent_task_missing")

            # 2. 幂等检查
            idem_key = incremental_artifact_idempotency_key(
                source_task_public_id=agent_task.public_id,
                source_artifact_public_id=source_artifact.public_id,
                source_artifact_version_no=source_artifact.version_no,
                incremental_modification_id=state.get("modification_idempotency_key") or state.get("task_id") or "fallback",
            )
            existing = find_idempotent_record(
                session=session, agent_task=agent_task,
                idempotency_key=idem_key,
            )
            if existing is not None:
                # 已存在同一 idempotency_key 写入 —— 返回旧 record 的 state delta
                return _idem_replay_delta(
                    existing_record=existing,
                    source_artifact=source_artifact,
                )

            # 3. 调 Phase 2.1 regenerate_sections_node 主体(字节级复用)
            fallback_state = _build_incremental_regen_state(
                state,
                failure_reason=failure_reason,
            )
            regen_result = await _invoke_regen_node(state=fallback_state, ctx=ctx)
            if not regen_result.get("success"):
                return _fail_delta(failure_reason + ":regen_failed")

            # 4. 调 WordExportTool 写 version_no+1
            new_public_id = f"artifact-{_gen_short_id()}"
            storage_path = (
                regen_result.get("storage_path")
                or getattr(source_artifact, "storage_path", None)
            )

            new_artifact = await create_incremental_artifact_record(
                session=session,
                source_artifact=source_artifact,
                new_public_id=new_public_id,
                storage_path=storage_path,
                idempotency_key=idem_key,
                owner_user_id=agent_task.user_id,
            )

            # 5. CAS 写 task_context_json
            ctx_version = int(
                (agent_task.task_context_json or {}).get("context_version", 0) or 0,
            )
            append_superseded_to_task_context(
                session=session,
                agent_task=agent_task,
                superseded_internal_id=source_artifact.id,
                idempotency_key=idem_key,
                new_artifact_internal_id=new_artifact.id,
                expected_context_version=ctx_version,
            )

            await session.commit()

            return {
                "incremental_result": {
                    "success": True,
                    "fallback_used": True,
                    "fallback_reason": failure_reason,
                    "new_artifact_public_id": new_artifact.public_id,
                    "new_artifact_version_no": new_artifact.version_no,
                    "source_artifact_internal_id": source_artifact.id,
                    "modified_section_ids": regen_result.get("modified_section_ids", []),
                    "tool_calls_used": regen_result.get("tool_calls_used", 0),
                    "rounds_used": 1,
                    "public_summary": {
                        "headline": "已通过降级流程生成新版本测试方案",
                        "detail": (
                            f"原方案 v{source_artifact.version_no} 因"
                            f"{failure_reason[:80]}无法继续,"
                            f"已用 Phase 2.1 全量重写生成 v{new_artifact.version_no}。"
                        ),
                    },
                },
                "incremental_steps": [
                    {
                        "action": "finish",
                        "outcome": "fallback",
                        "tool_name": "WordExportTool",
                        "decision_summary": "fallback to legacy regen + re-export",
                    },
                ],
            }
    except Exception as exc:  # noqa: BLE001
        _logger.exception(
            "incremental_fallback_crashed reason=%s",
            failure_reason,
            extra=logger_extra,
        )
        return _fail_delta(f"{failure_reason}:crash:{type(exc).__name__}")


# ── Helpers ─────────────────────────────────────────────────────


def _fail_delta(reason: str) -> Dict[str, Any]:
    return {
        "incremental_result": {
            "success": False,
            "fallback_used": True,
            "fallback_reason": reason[:240],
            "public_summary": {
                "headline": "增量任务无法完成",
                "detail": f"降级流程失败:{reason[:240]}。",
            },
        },
    }


def _idem_replay_delta(
    *,
    existing_record: Dict[str, Any],
    source_artifact,
) -> Dict[str, Any]:
    return {
        "incremental_result": {
            "success": True,
            "fallback_used": False,
            "fallback_reason": None,
            "new_artifact_internal_id": existing_record.get("new_artifact_internal_id"),
            "source_artifact_internal_id": existing_record.get("superseded_internal_id"),
            "idempotent_replay": True,
            "public_summary": {
                "headline": "已加载上一次增量结果(idempotent replay)",
                "detail": "本任务此前已写入相同 artifact,本次直接复用。",
            },
        },
    }


async def _invoke_regen_node(state: Dict[str, Any], *, ctx) -> Dict[str, Any]:
    """调用 Phase 2.1 ``regenerate_sections_node`` 主体。

    Phase 2.5 内:此处直接 import,但失败兜底为空 dict(保证 fallback 不抛)。
    """
    try:
        from app.agent_runtime.graphs.test_plan.versions.v2.nodes_review_format import (
            regenerate_sections_node,
        )
        with _temporary_ctx_intermediate_state(ctx, state):
            delta = await regenerate_sections_node(state, ctx=ctx)
        artifact = delta.get("artifact") or {}
        return {
            "success": True,
            "storage_path": artifact.get("storage_path") or artifact.get("storage_url"),
            "modified_section_ids": (delta.get("test_plan_content") or {}).get(
                "modified_section_ids", []
            ),
            "tool_calls_used": 1,
        }
    except Exception as exc:  # noqa: BLE001
        _logger.warning("regen_node_failed exc=%s", exc, exc_info=True)
        return {"success": False, "error": str(exc)[:240]}


def __select_artifact_by_public_id(public_id: str):
    """延迟构造 SQLAlchemy select,避免 import-time 循环。"""
    from sqlalchemy import select
    from app.models.artifact import Artifact

    return select(Artifact).where(Artifact.public_id == public_id)


def _gen_short_id() -> str:
    import secrets
    return secrets.token_hex(8)


_LEGACY_STATE_SEED_FIELDS: tuple[str, ...] = (
    "requirement_analysis",
    "template_structure",
    "knowledge_search_result",
    "user_prompt",
    "section_suggestions",
    "section_confirm_config",
    "template_file_id",
    "test_plan_content",
    "review_standard",
    "review_result",
    "artifact",
    "format_check_result",
    "pending_format_losses",
    "format_loss_confirmation",
)


@contextmanager
def _temporary_ctx_intermediate_state(ctx, state: Dict[str, Any]):
    """Seed RuntimeContext for legacy v2 nodes during incremental fallback.

    The v2 regen node still invokes ``ToolAdapter.execute`` without
    ``graph_state``. In normal v3 execution that node is wrapped by LangGraph
    state handling, but incremental fallback calls it directly. Seeding
    ``ctx._intermediate_state`` keeps that legacy node supplied with the same
    business context without changing unrelated v2 behavior.
    """
    seed = {
        key: state.get(key)
        for key in _LEGACY_STATE_SEED_FIELDS
        if state.get(key) is not None
    }
    previous = getattr(ctx, "_intermediate_state", None)
    if isinstance(previous, dict):
        backup = dict(previous)
        previous.update(seed)
        try:
            yield
        finally:
            previous.clear()
            previous.update(backup)
        return

    sentinel = object()
    old_value = previous if hasattr(ctx, "_intermediate_state") else sentinel
    try:
        setattr(ctx, "_intermediate_state", dict(seed))
    except Exception:
        yield
        return
    try:
        yield
    finally:
        try:
            if old_value is sentinel:
                delattr(ctx, "_intermediate_state")
            else:
                setattr(ctx, "_intermediate_state", old_value)
        except Exception:
            _logger.debug(
                "temporary ctx intermediate state restore skipped",
                exc_info=True,
            )


def _build_incremental_regen_state(
    state: Dict[str, Any],
    *,
    failure_reason: str,
) -> Dict[str, Any]:
    """Build a legacy-regeneration compatible state from IncrementalIntent.

    The v2 ``regenerate_sections_node`` only knows how to read
    ``review_result.review_issues``. Incremental tasks instead carry the user
    request in ``incremental_intent.scope``. If the normal incremental loop
    falls back, synthesize precise block issues so RegenTool receives
    non-empty ``section_ids`` / ``issues`` and can still do scoped repair.
    """
    next_state = dict(state)
    review_result = next_state.get("review_result")
    existing_issues = []
    if isinstance(review_result, dict):
        raw_issues = review_result.get("review_issues") or review_result.get("block_issues")
        if isinstance(raw_issues, list):
            existing_issues = [i for i in raw_issues if isinstance(i, dict)]

    if existing_issues:
        return next_state

    intent = next_state.get("incremental_intent")
    section_ids = _extract_incremental_target_section_ids(intent)
    request_text = _extract_incremental_request_text(intent)
    issues = [
        {
            "issue_id": f"incremental_modify_{section_id}",
            "rule_id": "incremental_user_request",
            "section_id": section_id,
            "severity": "block",
            "message": request_text or failure_reason,
            "repairable": True,
            "suggested_strategy": "regenerate_section",
            "kind": "incremental_modification",
            "evidence": {"fallback_reason": failure_reason[:240]},
        }
        for section_id in section_ids
        if section_id
    ]
    next_state["review_result"] = {
        "level": "failed",
        "review_issues": issues,
        "block_issues": issues,
        "issues": issues,
    }
    return next_state


def _extract_incremental_target_section_ids(intent: Any) -> List[str]:
    scope = _get_intent_attr(intent, "scope") or {}
    value = _get_intent_attr(scope, "target_section_ids") or []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _extract_incremental_request_text(intent: Any) -> str:
    scope = _get_intent_attr(intent, "scope") or {}
    text = _get_intent_attr(scope, "request_text") or _get_intent_attr(intent, "raw_user_message")
    return str(text or "")[:2000]


def _get_intent_attr(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


__all__ = ["run_legacy_incremental_fallback"]

# module-level note (auto-appended):
# Incremental fallback — 失败兜底。
# 关键约束: 回退到主流程(走完整 pre-confirm → post-confirm),不静默。
