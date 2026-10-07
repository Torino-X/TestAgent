"""Agent task service — task lifecycle, events, confirmation, cancel, retry.

════════════════════════════════════════════════════════════════════════════════
链路位置 (TestPlan 任务的持久化主入口):

  TestPlan 触发:
    api/v1/messages.py → MessageService.send_message(...) 检测到 prompt 触发
      → AgentTaskService.create_task(...)
        → 落库 AgentTask 行(started_at + task_id + status=pending)
        → AgentTaskService.dispatch_new_task(...)
          → Outbox: AgentExecutionRequest 行(供 Worker 拉取)
        → 返回 task_public_id 给前端,前端订阅 SSE

  任务执行:
    AgentExecutionWorker.cycle
      → claim_next(lease_owner)
        → AgentTaskService.bind_agent_run(task_public_id, run_id) 锁 run↔task
      → ApiDispatcher.dispatch_*(...)
        → LangGraph 跑 nodes,产 agent_event → EventRepository.save

  Resume (Section / Format interrupt):
    ApiDispatcher.dispatch_resume(task_public_id, decision_payload, source)
      → 写 AgentEvent(TASK_RESUMED)+ 校验 resume payload
      → LangGraphRunCoordinator.resume_section_confirmation(resume)

  Cancel:
    api/v1/agent_tasks.py → AgentTaskService.cancel_task(task_public_id)
      → 落 status=cancelled + emit TASK_CANCELLED
      → 触发 LangGraphRunCoordinator.cancel_signal

关键约束(供开发者速查):
  - 所有 task/public_id 都是字符串(public_id),内部数据库 ID 严格隔离;
  - EventRepository.save 必须 commit 才能被 SSE 订阅看到;
  - run_id 在 bind_agent_run 里持久化,后续 dict / dump 给前端用;
  - 不直接 emit SSE — 通过 EventRepository 让 LiveEventBus 推送;
  - retry: 不在这里实现,只在 Outbox 层做;AgentTaskService 只产请求。
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_event import AgentEvent
from app.models.agent_task import AgentTask
from app.models.human_confirmation import HumanConfirmation
from app.repositories.agent_task_repository import AgentTaskRepository
from app.repositories.agent_run_repository import AgentRunRepository
from app.repositories.confirmation_repository import ConfirmationRepository
from app.repositories.event_repository import EventRepository
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id
from app.utils.task_timing import duration_ms, to_rfc3339
from app.common.json_utils import normalize_json_object


def build_retry_task_context(
    task_context_json: object,
    *,
    engine_type: str,
    graph_version: str | None,
) -> dict:
    """Return retry context with a valid, immutable CE task manifest.

    A retry creates a new task.  Normally it inherits the source task's
    frozen manifest verbatim.  Tasks damaged by the former section-confirm
    overwrite bug have no manifest at all; copying that value would make the
    new task fall into the all-false compatibility profile again.  In that
    exceptional case, freeze the configuration at *retry creation* instead.
    """
    context = normalize_json_object(task_context_json)

    from app.context_engine.freeze.service import extract_manifest

    if extract_manifest(context) is not None:
        return context

    from app.context_engine.feature_flags import get_context_engine_flags
    from app.context_engine.freeze.profiles import snapshot_task_semantic_flags
    from app.context_engine.freeze.service import build_manifest, write_once_validate

    manifest = build_manifest(
        engine=engine_type,
        decision_reason="context_flags.freeze_at_retry",
        canary_bucket=None,
        workspace_key=None,
        context_engine_version=graph_version or "v3",
        task_semantic_flags=snapshot_task_semantic_flags(get_context_engine_flags()),
    )
    write_once_validate(manifest)
    context["frozen_flags_manifest"] = manifest
    return context


def _rfc3339_or_str(value) -> str | None:
    """Phase 2.9A.35: created_at 统一输出 RFC3339 UTC。

    value 可能是 datetime(ORM / dict 行)、带时区字符串、或无时区字符串。
    无时区字符串按 UTC 处理(历史契约)。
    """
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        # 已经是 ISO/UTC 字符串 → 原样返回
        if raw.endswith("Z") or "+00:00" in raw or ("-" in raw and ":" in raw and "T" in raw):
            return raw
        # 无时区 "YYYY-MM-DD HH:mm:ss" → 补 Z 输出 RFC3339
        return raw.replace(" ", "T") + "Z"
    try:
        return to_rfc3339(value)
    except Exception:  # noqa: BLE001
        return str(value)


class AgentTaskService:
    """Manages Agent task state and user interactions — real DB reads/writes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._task_repo = AgentTaskRepository(session)
        self._run_repo = AgentRunRepository(session)
        self._event_repo = EventRepository(session)
        self._confirm_repo = ConfirmationRepository(session)
        # Phase 2.9A.21:artifact repo 共享同一 session
        from app.repositories.artifact_repository import ArtifactRepository
        self._art_repo = ArtifactRepository(session)
        # Phase 2.9A.26+: conversation repo for public_id→internal_id lookups
        # used by list_tasks_for_conversation.
        from app.repositories.conversation_repository import ConversationRepository
        self._conv_repo = ConversationRepository(session)

    async def get_task(self, task_public_id: str, user_internal_id: int) -> dict:
        """Phase 2.9A.21 §7.2 任务详情扩展:返回 artifact + format_check_result。

        从 artifact 表拉当前任务最新一条 test_plan_word (task+user 隔离);
        从 review_issues / format_check_result 字典提取规范形式 — 不改变
        AgentTaskService 上下文 schema,只是把这两个 GraphState 实体重新
        暴露给前端以便 SSE 重连/历史恢复后能够重建任务面板。
        """
        task = await self._task_repo.get_by_public_id(task_public_id)
        if not task or task.user_id != user_internal_id:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("任务")

        run = None
        if task.active_run_id:
            # ``active_run_id`` stores the public run id (for example
            # ``run_...``), while ``get_by_internal_id`` expects the numeric
            # database primary key. Use the public-id lookup first so the
            # authoritative timing metadata is actually restored.
            run = await self._run_repo.get_by_public_id(task.active_run_id)
        if run is None:
            runs = await self._run_repo.list_by_task(task.id, limit=1)
            run = runs[0] if runs else None

        # Phase 2.9A.21:附加 artifact / format_check 详情 — 与前置
        # _to_detail 互不破坏。
        artifact_info = await self._summarize_latest_artifact(task.id, user_internal_id)
        format_check_info = await self._summarize_latest_format_check(task.id)

        # Phase 2.9A.30: trigger_message_id legacy fallback (返回 public_id)
        _internal_id, trigger_message_id = await self.resolve_trigger_message_id(task)

        return self._to_detail(
            task,
            run,
            artifact_info=artifact_info,
            format_check_info=format_check_info,
            trigger_message_id=trigger_message_id,
        )

    async def _summarize_latest_artifact(
        self, task_internal_id: int, user_internal_id: int
    ) -> dict | None:
        """Phase 2.9A.21:返回当前任务最新 test_plan_word Artifact 摘要;无则 None。

        防御性:任何子调用异常都不应阻塞 get_task。
        """
        try:
            artifacts = await self._art_repo.list_by_task(
                user_internal_id, task_internal_id
            )
        except Exception:  # noqa: BLE001 - 兜底
            return None
        if not artifacts:
            return None
        a = artifacts[0]
        metadata = a.metadata_json if isinstance(a.metadata_json, dict) else {}
        return {
            "artifact_id": a.public_id,
            "artifact_type": a.artifact_type,
            "file_name": a.file_name,
            "file_ext": a.file_ext,
            "file_size": a.file_size,
            "page_count": metadata.get("page_count"),
            "status": a.status,
            "version_no": a.version_no,
            "created_at": a.created_at.isoformat() if a.created_at else "",
        }

    async def _summarize_latest_format_check(
        self, task_internal_id: int
    ) -> dict | None:
        """Phase 2.9A.21:从 Review / FormatCheck 历史事件提取最近一次格式检查结果。

        优先用 format_check_result event payload,其次 review_result,最后 None。
        """
        try:
            events = await self._event_repo.list_by_task(task_internal_id)
        except Exception:  # noqa: BLE001 - 兜底
            return None
        # Phase 2.9A.26: list_by_task returns dicts (not ORM).
        for event in reversed(events):
            event_type = event.get("event_type") if isinstance(event, dict) else event.event_type
            if event_type != "docx_format_checked":
                continue
            payload = event.get("payload_json") if isinstance(event, dict) else event.payload_json
            if isinstance(payload, dict):
                return {
                    "level": payload.get("status") or payload.get("level"),
                    "losses": payload.get("losses") or [],
                    "loop_count": payload.get("loop_count"),
                    "checked_artifact_public_id": payload.get("checked_artifact_public_id"),
                }
        return None

    async def resolve_trigger_message_id(
        self,
        task,
    ) -> tuple[int | None, str | None]:
        """Phase 2.9A.30: trigger_message_id legacy fallback。

        返回 (internal_id, public_id) 元组。
        API 层使用 public_id (字符串); 内部逻辑使用 internal_id (整数)。

        Fallback 规则:
          * 若 ``task.trigger_message_id`` 已存在(新任务),解析 public_id;
          * 否则在同一 ``conversation_id`` 内,选 ``created_at <= task.created_at``
            且 ``role='user'`` ``message_type='user_text'`` 的最近一条消息。
          * 找不到返回 (None, None)。

        不修改 DB;只覆盖返回字段,避免污染历史数据。
        """
        if getattr(task, "trigger_message_id", None) is not None:
            internal_id = int(task.trigger_message_id)
            try:
                from sqlalchemy import text as _sa_text
                row = (
                    await self._session.execute(
                        _sa_text(
                            "SELECT public_id FROM messages WHERE id = :mid"
                        ),
                        {"mid": internal_id},
                    )
                ).first()
                public_id = str(row[0]) if row else None
                return internal_id, public_id
            except Exception:  # noqa: BLE001
                return internal_id, None
        try:
            from sqlalchemy import text as _sa_text

            row = (
                await self._session.execute(
                    _sa_text(
                        "SELECT id, public_id FROM messages "
                        "WHERE conversation_id = :cid "
                        "AND role = 'user' AND message_type = 'user_text' "
                        "AND created_at <= :tcreated "
                        "ORDER BY created_at DESC, id DESC LIMIT 1"
                    ),
                    {
                        "cid": int(task.conversation_id),
                        "tcreated": task.created_at,
                    },
                )
            ).first()
        except Exception:  # noqa: BLE001 - 任何 DB 异常都吞掉,不阻塞任务详情
            return None, None
        if row is None:
            return None, None
        return int(row[0]), str(row[1])

    async def list_events(
        self,
        task_public_id: str,
        user_internal_id: int,
        *,
        cursor: int | None = None,
        limit: int = 50,
    ) -> dict:
        """Phase 2.9A.27: 支持 cursor 分页与真实 total。

        既有 caller (orchestrator SSE retry 等) 仍按 ``list[dict]`` 取 events;
        新增分页字段 ``next_cursor`` / ``total`` 仅放在 dict 里,不影响既有
        直接遍历 events 的逻辑。

        Returns:
            ``{"events": [...], "next_cursor": int|None, "total": int}``
        """
        task = await self._task_repo.get_by_public_id(task_public_id)
        if not task or task.user_id != user_internal_id:
            return {"events": [], "next_cursor": None, "total": 0}
        events, next_cursor, total = await self._event_repo.list_by_task(
            task.id,
            limit=limit,
            cursor=cursor,
            return_total=True,
        )
        summaries = [
            {**self._event_to_summary(event), "task_id": task_public_id}
            for event in events
        ]
        return {
            "events": summaries,
            "next_cursor": next_cursor,
            "total": total,
        }

    async def list_tasks_for_conversation(
        self, user_internal_id: int, conv_public_id: str
    ) -> list[dict]:
        """Phase 2.9A.26+: list all tasks for a conversation in stable
        chronological order.  Used by the conversation detail endpoint
        so the front-end can render multiple task run blocks instead
        of only the latest one.

        Phase 2.9A.27: 每个 task 都走 ``resolve_trigger_message_id``
        legacy fallback,旧任务(null)自动用最近 user_text 消息补全。

        Phase 2.9A.30: 返回 public_id (字符串),不是 internal id。
        """
        conv = await self._conv_repo.get_by_public_id(conv_public_id)
        if not conv or conv.user_id != user_internal_id:
            return []
        tasks = await self._task_repo.list_by_conversation(user_internal_id, conv.id)
        out: list[dict] = []
        for t in tasks:
            run = None
            if getattr(t, "active_run_id", None):
                run = await self._run_repo.get_by_public_id(t.active_run_id)
            if run is None:
                runs = await self._run_repo.list_by_task(t.id, limit=1)
                run = runs[0] if runs else None
            _internal_id, trigger_message_id = await self.resolve_trigger_message_id(t)
            detail = self._to_detail(
                t,
                run,
                artifact_info=None,
                format_check_info=None,
                trigger_message_id=trigger_message_id,
            )
            detail["engine_type"] = t.engine_type
            out.append(detail)
        return out

    async def confirm_sections(self, task_public_id: str, sections: list[dict], user_internal_id: int) -> dict:
        task = await self._task_repo.get_by_public_id(task_public_id)
        if not task or task.user_id != user_internal_id:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("任务")

        # Find the pending confirmation and mark it confirmed
        pending = await self._confirm_repo.get_pending_by_task(task.id)
        if pending:
            await self._confirm_repo.confirm(pending.id, {"sections": sections}, utcnow())

        # Update task status to running (resume after confirmation)
        await self._task_repo.update_status(task.id, "running")
        await self._session.flush()

        # P0 收口:cache write-through 必须严格在 commit 之后.
        # 用 ``register_after_commit`` 挂 hook;rollback 时不触发,保证 Redis
        # 不领先于已提交的 MySQL 状态. 上层 endpoint 用 FastAPI get_db 在
        # 依赖 teardown 时 commit.
        try:
            from app.cache.domains.task_cache import get_task_status_cache
            from app.db.sync import register_after_commit

            _active_run_id = getattr(task, "active_run_id", None)
            _task_public_id = task.public_id
            _now = utcnow()

            async def _do_write_through(_session):
                try:
                    await get_task_status_cache().write_through(
                        task_public_id=_task_public_id,
                        status="running",
                        active_run_id=_active_run_id,
                        updated_at=_now,
                    )
                except Exception as cache_exc:  # noqa: BLE001
                    import logging
                    logging.getLogger(__name__).warning(
                        "AgentTaskService.confirm_sections: task status cache "
                        "write-through failed | task_id=%s | %s",
                        _task_public_id, cache_exc,
                    )

            register_after_commit(self._session, _do_write_through)
        except Exception as exc:  # noqa: BLE001 — cache register must not fail confirm
            import logging
            logging.getLogger(__name__).warning(
                "AgentTaskService.confirm_sections: cache hook register failed | "
                "task_id=%s | %s",
                task.public_id, exc,
            )

        # Record event
        now = utcnow()
        event = AgentEvent(
            public_id=generate_public_id("event"),
            user_id=user_internal_id,
            conversation_id=task.conversation_id,
            task_id=task.id,
            event_type="task_resumed",
            message_type="task_resumed",
            title="用户已确认，继续执行",
            content="用户已完成章节确认，Agent 将继续生成测试方案",
            status="created",
            created_at=now,
        )
        await self._event_repo.create(event)

        return {"task_id": task_public_id, "status": "running"}

    async def get_pending_confirmation(self, task_public_id: str, user_internal_id: int) -> dict | None:
        task = await self._task_repo.get_by_public_id(task_public_id)
        if not task or task.user_id != user_internal_id:
            return None
        pending = await self._confirm_repo.get_pending_by_task(task.id)
        if not pending:
            return None
        request_json = (
            pending.request_json if isinstance(pending.request_json, dict) else {}
        )
        return {
            "confirmation_id": pending.public_id,
            "confirmation_type": pending.confirmation_type,
            "status": pending.status,
            "sections": request_json.get("sections", []),
            "cards": request_json.get("cards", []),
            "retrieval_summary": request_json.get("retrieval_summary", {}),
        }

    async def list_confirmed_confirmations(
        self, task_public_id: str, user_internal_id: int
    ) -> list[dict] | None:
        """Return persisted confirmation decisions for timeline hydration.

        Confirmation receipts are presentation state, but the decisions they
        describe are durable business data in ``human_confirmations``.  A
        completed task no longer has a pending checkpoint, so history restore
        must query confirmed rows explicitly instead of relying on SSE events.
        """
        task = await self._task_repo.get_by_public_id(task_public_id)
        if not task or task.user_id != user_internal_id:
            return None
        confirmations = await self._confirm_repo.list_confirmed_by_task(task.id)
        return [
            {
                "confirmation_id": confirmation.public_id,
                "confirmation_type": confirmation.confirmation_type,
                "status": confirmation.status,
                "request": confirmation.request_json or {},
                "response": confirmation.response_json or {},
                "confirmed_at": (
                    confirmation.confirmed_at.isoformat()
                    if confirmation.confirmed_at is not None
                    else None
                ),
            }
            for confirmation in confirmations
        ]

    async def cancel(self, task_public_id: str, user_internal_id: int) -> dict:
        # 2026-07-14：orchestrator 长事务可能持 agent_tasks 行锁几十秒
        # （checkpoint 写入 + LLM 调用）。先用 SELECT ... FOR UPDATE NOWAIT
        # 试探：拿不到锁立刻抛 409，让前端知道"任务忙，请稍后重试"，
        # 避免前端 cancel 静默 hang 50s 后才抛 1205。
        from sqlalchemy import text
        try:
            row = await self._session.execute(
                text(
                    "SELECT id FROM agent_tasks "
                    "WHERE public_id = :pid AND deleted_at IS NULL "
                    "FOR UPDATE NOWAIT"
                ),
                {"pid": task_public_id},
            )
            task_id_row = row.fetchone()
            if not task_id_row:
                from app.core.exceptions import NotFoundError
                raise NotFoundError("任务")
            task_internal_id = task_id_row[0]
        except Exception as exc:
            # MySQL 8 NOWAIT 锁不到 → 抛出 3572（"Statement aborted because
            # lock(s) could not be acquired immediately and NOWAIT is set."）
            # PyMySQL 包成 OperationalError(3572, ...)。
            err_code = getattr(exc.orig, "args", [None])[0] if hasattr(exc, "orig") else None
            if err_code == 3572 or "NOWAIT" in str(exc):
                from app.core.exceptions import AppError
                raise AppError(
                    40901,
                    "任务正在执行关键步骤，请稍后再试取消（或等待任务自然结束）",
                ) from exc
            raise

        task = await self._task_repo.get_by_public_id(task_public_id)
        if not task or task.user_id != user_internal_id:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("任务")

        await self._task_repo.update_status(task_internal_id, "cancelled")
        # cancel 端的事务立即 commit，让上面 NOWAIT 拿到的锁立即释放，
        # 不让 cancel 自己也变成持锁事务阻塞 orchestrator 的下一波 UPDATE
        await self._session.commit()

        # Phase 1 (Step 6): write-through terminal status (6h TTL).
        # cancelled 是 terminal 状态，写穿后 SSE heartbeat 在 6h 内一直
        # 命中缓存（设计文档 §13.4）;6h 后过期，符合"任务结束就不再变化"语义.
        try:
            from app.cache.domains.task_cache import get_task_status_cache

            await get_task_status_cache().write_through(
                task_public_id=task_public_id,
                status="cancelled",
                active_run_id=getattr(task, "active_run_id", None),
                updated_at=utcnow(),
            )
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "AgentTaskService.cancel: task status cache write-through "
                "failed | task_id=%s | %s",
                task_public_id, exc,
            )

        now = utcnow()
        event = AgentEvent(
            public_id=generate_public_id("event"),
            user_id=user_internal_id,
            conversation_id=task.conversation_id,
            task_id=task_internal_id,
            event_type="task_cancelled",
            title="任务已取消",
            content="用户取消了任务",
            status="completed",
            created_at=now,
        )
        await self._event_repo.create(event)
        await self._session.commit()

        return {"task_id": task_public_id, "status": "cancelled"}

    async def retry(
        self,
        task_public_id: str,
        retry_mode: str,
        user_internal_id: int,
        *,
        user_instruction: str | None = None,
    ) -> dict:
        """Retry a failed task — creates a new task reusing the old one's
        checkpoint snapshot (when ``retry_mode='from_failed_step'``) so
        successful steps can be skipped on re-run.

        New task starts at status='created'. If ``user_instruction`` is
        supplied, it overrides the original prompt for the retry.
        """
        old_task = await self._task_repo.get_by_public_id(task_public_id)
        if not old_task or old_task.user_id != user_internal_id:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("任务")

        old_engine = str(old_task.engine_type or "").strip().lower()
        if old_engine != "langgraph":
            from app.core.exceptions import UnsupportedLegacyTaskError

            raise UnsupportedLegacyTaskError(task_public_id)

        # A retry is a new task. Never create a fresh task pinned to any
        # historical graph version, and never reinterpret an old frozen
        # manifest under v3. Operators can still read/export the historical
        # task and must start a genuinely new v3 task for further execution.
        if str(getattr(old_task, "graph_version", "") or "") != "v3":
            from app.core.exceptions import UnsupportedLegacyTaskError

            raise UnsupportedLegacyTaskError(task_public_id)

        if old_task.status not in ("failed", "cancelled", "completed"):
            from app.core.exceptions import AppError
            raise AppError(40900, "只有失败、取消或已完成的任务才能重试")

        # F024: reuse latest_snapshot_id only when retrying from the
        # failed step.  ``from_beginning`` discards the snapshot so the
        # full pipeline re-runs.
        inherit_snapshot = retry_mode != "from_beginning"
        latest_snapshot_id = old_task.latest_snapshot_id if inherit_snapshot else None
        effective_instruction = (
            user_instruction if (user_instruction and user_instruction.strip())
            else old_task.user_instruction
        )
        retry_task_context = build_retry_task_context(
            old_task.task_context_json,
            engine_type=old_engine,
            graph_version=old_task.graph_version,
        )

        now = utcnow()
        new_task = AgentTask(
            public_id=generate_public_id("task"),
            user_id=user_internal_id,
            conversation_id=old_task.conversation_id,
            project_id=getattr(old_task, "project_id", None),
            task_type=old_task.task_type,
            status="created",
            title=old_task.title,
            user_instruction=effective_instruction,
            plan_json=old_task.plan_json,
            task_context_json=retry_task_context,
            latest_snapshot_id=latest_snapshot_id,
            context_version=0,
            engine_type="langgraph",
            # A compatible v3 retry keeps the original frozen manifest and graph.
            graph_name=old_task.graph_name,
            graph_version=old_task.graph_version,
            created_at=now,
            updated_at=now,
        )
        new_task = await self._task_repo.create(new_task)

        # Write retry event on the old task
        event = AgentEvent(
            public_id=generate_public_id("event"),
            user_id=user_internal_id,
            conversation_id=old_task.conversation_id,
            task_id=old_task.id,
            event_type="task_retried",
            title="任务已重试",
            content=f"任务已重试(retry_mode={retry_mode})，新任务 ID: {new_task.public_id}",
            status="completed",
            created_at=now,
        )
        await self._event_repo.create(event)

        from app.repositories.agent_execution_request_repository import (
            AgentExecutionRequestRepository,
        )

        await AgentExecutionRequestRepository(self._session).enqueue_new_task(
            task_public_id=new_task.public_id,
            task_internal_id=new_task.id,
            engine_type="langgraph",
            request_type="new_task",
            graph_name=new_task.graph_name,
            graph_version=new_task.graph_version,
            payload={
                "user_instruction": effective_instruction,
                "retry_of_task_id": task_public_id,
                "retry_mode": retry_mode,
                "graph_name": new_task.graph_name,
                "graph_version": new_task.graph_version,
            },
            idempotency_key=f"retry|{task_public_id}|{new_task.public_id}",
        )

        return {
            "old_task_id": task_public_id,
            "new_task_id": new_task.public_id,
            "status": "created",
            "events_url": f"/api/agent/tasks/{new_task.public_id}/events",
        }

    @staticmethod
    def _to_detail(
        task,
        run=None,
        artifact_info: dict | None = None,
        format_check_info: dict | None = None,
        trigger_message_id: str | None = None,
    ) -> dict:
        started_at = (run.started_at if run else None) or task.started_at
        finished_at = (run.finished_at if run else None) or task.completed_at
        run_payload = None
        if run is not None:
            run_payload = {
                "run_id": run.public_id,
                "status": run.status,
                "started_at": to_rfc3339(run.started_at),
                "finished_at": to_rfc3339(run.finished_at),
                "duration_ms": duration_ms(run.started_at, run.finished_at),
            }
        # Phase 2.9A.30: caller 已解析 public_id, 不再做 ORM fallback。
        engine_type = str(
            getattr(task, "engine_type", "langgraph") or ""
        ).strip().lower()
        is_terminal = task.status in {"completed", "failed", "cancelled"}

        return {
            "task_id": task.public_id,
            "task_type": task.task_type,
            "status": task.status,
            "plan": task.plan_json or [],
            "task_context": task.task_context_json,
            "review_result": task.review_result_json,
            "events_url": f"/api/agent/tasks/{task.public_id}/events",
            "active_run_id": run.public_id if run else getattr(task, "active_run_id", None),
            "runtime_status": getattr(task, "runtime_status", None),
            "engine_type": engine_type,
            "graph_name": getattr(task, "graph_name", None),
            "graph_version": getattr(task, "graph_version", None),
            "execution_supported": engine_type == "langgraph",
            "migration_status": (
                None
                if engine_type == "langgraph"
                else ("historical-read-only" if is_terminal else "migration-required")
            ),
            "started_at": to_rfc3339(started_at),
            "completed_at": to_rfc3339(finished_at),
            "duration_ms": duration_ms(started_at, finished_at),
            "run": run_payload,
            # Phase 2.9A.21 §7.2:扩展任务详情 — 前端 SSE 重连后能
            # 直接从这里重建 Artifact / FormatCheck 卡片,无须再调
            # /artifacts 接口或重新解析历史事件 payload。
            "artifact": artifact_info,
            "format_check_result": format_check_info,
            # Phase 2.9A.26+: anchor the task to the user message that
            # triggered it; consumed by the front-end timeline assembly.
            # Phase 2.9A.27: 旧任务 trigger_message_id NULL 时使用
            # ``resolve_trigger_message_id`` 的 legacy fallback。
            "trigger_message_id": trigger_message_id,
        }

    @staticmethod
    def _event_to_summary(event) -> dict:
        # Phase 2.9A.26: event is now a dict (not ORM) from list_by_task.
        # Both dict keys and ORM attribute access work identically.
        # Phase 2.9A.35: payload 必须返回 object(规范化为 dict),created_at
        # 必须带时区(RFC3339 UTC),禁止输出 JSON 字符串 / 无时区字符串。
        if isinstance(event, dict):
            created_at = event.get("created_at")
            return {
                "event_id": event.get("public_id", ""),
                "event_type": event.get("event_type", ""),
                "message_type": event.get("message_type"),
                "title": event.get("title"),
                "content": event.get("content"),
                "payload": normalize_json_object(event.get("payload_json")),
                "status": event.get("status"),
                "sequence_no": event.get("sequence_no"),
                "graph_run_id": event.get("graph_run_id"),
                "graph_version": event.get("graph_version"),
                "node_name": event.get("node_name"),
                "event_schema_version": event.get("event_schema_version"),
                "created_at": _rfc3339_or_str(created_at),
                "canonical_order": event.get("canonical_order"),
            }
        # Fallback for ORM objects (used by other callers).
        return {
            "event_id": event.public_id,
            "event_type": event.event_type,
            "message_type": event.message_type,
            "title": event.title,
            "content": event.content,
            "payload": normalize_json_object(event.payload_json),
            "status": event.status,
            "sequence_no": event.sequence_no,
            "graph_run_id": event.graph_run_id,
            "graph_version": event.graph_version,
            "node_name": event.node_name,
            "event_schema_version": event.event_schema_version,
            "created_at": _rfc3339_or_str(event.created_at),
        }
