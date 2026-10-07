"""ContextSnapshotWriterService — 统一生命周期（CAS/expected-status 守卫）。

CE-02 WP-7：

统一生命周期（严格时序）：
```
begin_build(status=building，写完整 compose metadata)
→ mark_ready(building → ready)
→ mark_sent(ready → sent)              # LLM 请求已提交 Provider
→ Provider 成功: complete(sent → completed)   # 无论 parser 结果
→ parser 失败: 原 Snapshot 保持 completed；Schema Retry 创建新 Snapshot
→ Provider 失败: fail(sent → failed)
→ Context Length Error: 原 Snapshot failed → compose_for_retry → 新 Snapshot
→ 取消: begin 前无 Snapshot；begin 后 sent 前 abandon(building/ready→abandoned)；
        sent 后尝试取消 Provider，按真实结果 completed/failed/abandoned
```

- 全部转换用 **CAS/expected status**：期望状态不符 → 非法转换拒绝。
- ``complete``：写 **actual** input/output tokens + LLM latency + completed_at；
  actual 只在真实 Provider usage 时写，否则 null，禁止用估算值冒充。
  **latency_json 严格映射 ContextBuildLatency，只含延迟字段；provider_request_id
  不写入 latency_json**（CE-02 整改二，表无独立列则不持久化）。
- ``fail``：status=failed + safe error_code（不保存 Provider 原始错误全文）。
- ``abandon``：building/ready → abandoned。
- **Shadow 快照（成功路径，非 abandoned）**：begin(context_kind='shadow',
  building) → attach → mark_ready → complete_shadow(ready→completed)。
  sent_at/provider_request_id/actual tokens=null，completed_at=当前时间。
  CAS：shadow ready→completed 允许；active ready→completed 拒绝；completed
  shadow 不可再 sent/fail/abandon。
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from typing import Any

from app.context_engine.errors import ContextEngineStage, raise_engine_error
from app.context_engine.models.snapshot_models import (
    ContextBuildLatency,
    ContextSnapshotBeginCommand,
    ContextSnapshotCompleteCommand,
    ContextSnapshotFailCommand,
    ContextSnapshotRef,
)

# 允许的状态转换（expected → next）
_TRANSITIONS: dict[str, set[str]] = {
    "building": {"ready", "abandoned"},
    "ready": {"sent", "abandoned", "completed"},  # ready→completed 仅 Shadow complete_shadow
    "sent": {"completed", "failed", "abandoned"},
}

# 终止状态
_TERMINAL = {"completed", "failed", "abandoned"}


class SnapshotStatusError(Exception):
    """快照状态转换非法（CAS 拒绝）。"""

    def __init__(self, detail: str, code: str = "context.snapshot.illegal_transition") -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


class ContextSnapshotWriterService:
    """Snapshot 生命周期写服务。"""

    def __init__(
        self,
        snapshot_repository_factory,
        *,
        engine_version: str = "1.0",
        now_factory=datetime.utcnow,
    ) -> None:
        self._repo_factory = snapshot_repository_factory
        self._engine_version = engine_version
        self._now = now_factory

    # ── begin ─────────────────────────────────────────────────────────

    async def begin_build(
        self,
        command: ContextSnapshotBeginCommand,
        *,
        session,
    ) -> ContextSnapshotRef:
        """begin_build：status=building，写完整 compose metadata。"""
        from app.models.context_snapshot import ContextSnapshot

        repo = self._repo_factory(session)
        snapshot = ContextSnapshot()
        snapshot.public_id = _gen_public_id()
        snapshot.user_id = command.user_id
        snapshot.conversation_id = command.conversation_id
        snapshot.agent_task_id = command.agent_task_id
        snapshot.llm_task_type = command.llm_task_type
        snapshot.context_kind = command.context_kind
        snapshot.engine_version = self._engine_version
        snapshot.call_site = command.call_site
        snapshot.context_profile_key = command.context_profile_key
        snapshot.context_profile_version = command.context_profile_version
        snapshot.context_policy_version = command.context_policy_version
        snapshot.model_config_id = command.model_config_id
        snapshot.model_name_snapshot = command.model_name_snapshot
        snapshot.context_window_tokens = command.context_window_tokens
        snapshot.input_budget_tokens = command.input_budget_tokens
        snapshot.output_reserve_tokens = command.output_reserve_tokens
        snapshot.runtime_reserve_tokens = command.runtime_reserve_tokens
        snapshot.safety_margin_tokens = command.safety_margin_tokens
        snapshot.target_input_tokens = command.target_input_tokens
        snapshot.estimated_input_tokens = command.estimated_input_tokens
        snapshot.section_stats_json = command.section_stats_json
        snapshot.included_refs_json = command.included_refs_json
        snapshot.dropped_refs_json = command.dropped_refs_json
        snapshot.prompt_digest = str(command.prompt_digest) if command.prompt_digest else None
        snapshot.prompt_excerpt = command.prompt_excerpt
        snapshot.latency_json = command.latency_json
        snapshot.included_message_ids = command.included_message_ids
        snapshot.included_file_ids = command.included_file_ids
        snapshot.included_task_ids = command.included_task_ids
        snapshot.included_artifact_ids = command.included_artifact_ids
        snapshot.included_knowledge_ids = command.included_knowledge_ids
        snapshot.summary_id = command.summary_id
        snapshot.retrieval_run_ids_json = (
            {"run_ids": command.retrieval_run_ids} if command.retrieval_run_ids else None
        )
        snapshot.compaction_json = command.preflight_json
        snapshot.status = "building"
        snapshot.estimated_tokens = command.estimated_input_tokens
        snapshot.context_digest = str(command.prompt_digest) if command.prompt_digest else None
        snapshot.created_at = _utcnow()

        await repo.create(snapshot)
        return ContextSnapshotRef(
            public_id=snapshot.public_id,
            status=snapshot.status,
            prompt_digest=snapshot.prompt_digest,
            context_kind=command.context_kind,
            execution_mode=command.context_kind,
        )

    # ── 状态转换（CAS/expected-status）─────────────────────────────────

    async def _transition(
        self,
        *,
        session,
        public_id: str,
        user_id: int,
        expected: set[str],
        next_status: str,
        set_fields: dict[str, Any] | None = None,
        where_extra: str | None = None,
        where_params: dict[str, Any] | None = None,
    ) -> ContextSnapshotRef | None:
        """CAS 转换：仅当当前 status ∈ expected 时更新为 next_status。

        返回新 ref；若 current status 不匹配（竞态 / 非法）返回 None。
        """
        from sqlalchemy import text

        expected_sql = ", ".join(f"'{e}'" for e in sorted(expected))
        set_clauses = ["status = :ns"]
        params: dict[str, Any] = {"ns": next_status, "pid": public_id, "uid": user_id}
        if set_fields:
            for col, value in set_fields.items():
                set_clauses.append(f"{col} = :f_{col}")
                params[f"f_{col}"] = value
        where_sql = "user_id = :uid AND public_id = :pid AND status IN (" + expected_sql + ")"
        if where_extra:
            where_sql += " AND " + where_extra
            params.update(where_params or {})

        result = await session.execute(
            text(
                f"UPDATE llm_context_snapshots SET {', '.join(set_clauses)} "
                f"WHERE {where_sql}"
            ),
            params,
        )
        if result.rowcount == 0:
            return None
        await session.flush()
        return ContextSnapshotRef(public_id=public_id, status=next_status)

    async def mark_ready(self, *, session, public_id: str, user_id: int) -> ContextSnapshotRef:
        ref = await self._transition(
            session=session,
            public_id=public_id,
            user_id=user_id,
            expected={"building"},
            next_status="ready",
        )
        if ref is None:
            raise SnapshotStatusError(f"snapshot {public_id} 不能从非 building 转 ready")
        return ref

    async def mark_sent(self, *, session, public_id: str, user_id: int) -> ContextSnapshotRef:
        ref = await self._transition(
            session=session,
            public_id=public_id,
            user_id=user_id,
            expected={"ready"},
            next_status="sent",
        )
        if ref is None:
            raise SnapshotStatusError(f"snapshot {public_id} 不能从非 ready 转 sent")
        return ref

    async def complete(
        self,
        command: ContextSnapshotCompleteCommand,
        *,
        session,
        user_id: int,
    ) -> ContextSnapshotRef:
        """complete：sent → completed。

        actual tokens 只在真实 Provider usage 时写，否则 null；
        禁止用估算值冒充 actual。latency_json 严格映射 ContextBuildLatency，
        只含延迟字段（CE-02 整改二：provider_request_id 不写入 latency_json）。
        """
        now = _utcnow()
        latency = {
            "provider_ms": command.llm_latency_ms,
        }
        ref = await self._transition(
            session=session,
            public_id=command.snapshot_public_id,
            user_id=user_id,
            expected={"sent"},
            next_status="completed",
            set_fields={
                "actual_input_tokens": command.actual_input_tokens,
                "actual_output_tokens": command.actual_output_tokens,
                "completed_at": command.completed_at or now,
                "latency_json": _json.dumps(latency, ensure_ascii=False, default=str),
            },
        )
        if ref is None:
            raise SnapshotStatusError(f"snapshot {command.snapshot_public_id} 不能从非 sent 转 completed")
        return ref

    async def fail(
        self,
        command: ContextSnapshotFailCommand,
        *,
        session,
        user_id: int,
    ) -> ContextSnapshotRef:
        """fail：sent → failed（安全 error_code，不保存原始 Provider 错误全文）。"""
        now = _utcnow()
        ref = await self._transition(
            session=session,
            public_id=command.snapshot_public_id,
            user_id=user_id,
            expected={"sent", "building", "ready"},
            next_status="failed",
            set_fields={
                "error_code": command.error_code,
                "completed_at": command.failed_at or now,
            },
        )
        if ref is None:
            raise SnapshotStatusError(f"snapshot {command.snapshot_public_id} 不能转 failed")
        return ref

    async def abandon(self, *, session, public_id: str, user_id: int) -> ContextSnapshotRef:
        """abandon：building/ready → abandoned（begin 后、sent 前取消）。"""
        ref = await self._transition(
            session=session,
            public_id=public_id,
            user_id=user_id,
            expected={"building", "ready"},
            next_status="abandoned",
        )
        if ref is None:
            raise SnapshotStatusError(f"snapshot {public_id} 不能转 abandoned")
        return ref

    # ── Shadow（成功路径，非 abandoned）────────────────────────────────

    async def complete_shadow(
        self,
        *,
        session,
        public_id: str,
        user_id: int,
        completed_at: datetime | None = None,
    ) -> ContextSnapshotRef:
        """Shadow：ready → completed。

        sent_at / provider_request_id / actual tokens = null，completed_at=当前时间。
        CAS：shadow ready→completed 允许；active ready→completed 拒绝
        （active 无 sent 不得 complete）。
        """
        from sqlalchemy import text

        # CAS：仅当 context_kind='shadow' 且 status='ready'
        result = await session.execute(
            text(
                "UPDATE llm_context_snapshots SET status = 'completed', completed_at = :ca "
                "WHERE public_id = :pid AND user_id = :uid "
                "AND context_kind = 'shadow' AND status = 'ready'"
            ),
            {"ca": completed_at or _utcnow(), "pid": public_id, "uid": user_id},
        )
        if result.rowcount == 0:
            raise SnapshotStatusError(
                f"shadow snapshot {public_id} 不能从非 ready 转 completed（或非 shadow）"
            )
        await session.flush()
        return ContextSnapshotRef(public_id=public_id, status="completed", context_kind="shadow", execution_mode="shadow")


def _gen_public_id() -> str:
    import uuid

    return "cs_" + uuid.uuid4().hex[:40]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
# auto-appended module-level note: snapshot service: save + load ContextEngine state 快照。
