"""Production-only RuntimeContext construction for LangGraph executions.

The graph checkpoint stores only serializable state.  This module rebuilds the
live collaborators that must never be checkpointed: database sessions, the
event sink, tool adapter, user settings service, and cancellation service.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime
from typing import Any, Callable, Dict

from app.agent_runtime.adapters.test_agent_tool_adapter import TestAgentToolAdapter
from app.agent_runtime.cancellation_distributed import DistributedCancellationService
from app.agent_runtime.events.live_agent_event_sink import LiveAgentEventSink
from app.agent_runtime.events.sequence_allocator import SequenceNumberAllocator
from app.agent_runtime.runtime_context import RuntimeContext
from app.integrations.llm_client import LLMClient
from app.repositories.event_repository import EventRepository
from app.services.settings_service import SettingsService
from app.tools.executor import ToolExecutor
from app.tools.registry import tool_registry
from app.agent_runtime.tool_output_governance import ProductionToolOutputRecorder
from app.context_engine.payload.factory import build_payload_backend
from app.context_engine.payload.payload_storage import PayloadStorageService
from app.context_engine.tool_output import ToolOutputManager
from app.repositories.context_engine_repositories import ContextPayloadRepository
from app.repositories.tool_call_repository import ToolCallRepository


logger = logging.getLogger(__name__)


async def _build_llm_client_for_user(
    settings_service: "_SessionScopedSettingsService",
    user_internal_id: int,
) -> "LLMClient | None":
    """Build an LLMClient wired to the user's configured model.

    Phase 2.9B.1: dynamic subgraphs (Preparation/Repair/Incremental) need a
    real decision LLM.  The provider is resolved per-user at invoke time; when
    the user has no model_configs row the provider is an
    ``LLMNotConfiguredMarker`` — LLMClient surfaces that as a friendly error
    that the subgraph fallback handles.  Returns None on any unexpected
    failure so the dynamic node can fall back deterministically instead of
    crashing the main graph.
    """
    try:
        provider = await settings_service.build_llm_config_provider(
            user_id=user_internal_id
        )
        return LLMClient(config_provider=provider)
    except Exception as exc:  # noqa: BLE001 — never crash graph invoke
        logger.warning(
            "llm_client build failed | user=%d | err=%s",
            user_internal_id,
            type(exc).__name__,
        )
        return None


class _SessionScopedEventRepository:
    """Open a short-lived session for every event persistence operation."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    async def create_with_idempotency(
        self,
        *,
        event_id: str | None,
        idempotency_key: str | None,
        event: Any,
    ) -> bool:
        async with self._session_factory() as session:
            try:
                created = await EventRepository(session).create_with_idempotency(
                    event_id=event_id,
                    idempotency_key=idempotency_key,
                    event=event,
                )
                await session.commit()
                return created
            except Exception:
                await session.rollback()
                raise


class _SessionScopedSettingsService:
    """Expose SettingsService without retaining a request-bound session."""

    def __init__(self, session_factory: Callable[[], Any], user_internal_id: int) -> None:
        self._session_factory = session_factory
        self._user_internal_id = user_internal_id

    async def build_llm_config_provider(self, user_id: int | None = None) -> Any:
        return await self._call(
            "build_llm_config_provider", user_id or self._user_internal_id
        )

    async def build_image_understanding_provider(
        self, user_id: int | None = None
    ) -> Any:
        return await self._call(
            "build_image_understanding_provider", user_id or self._user_internal_id
        )

    async def llm_config_provider(self) -> Any:
        return await self.build_llm_config_provider(self._user_internal_id)

    async def tool_card_narrative_enabled(self) -> bool:
        """Read the persisted user preference before a tool narrative LLM call."""
        from app.agent_runtime._shared.narrative_governance.settings_service import (
            NarrativeSettingsService,
        )

        async with self._session_factory() as session:
            return await NarrativeSettingsService(session).get_user_enabled(
                self._user_internal_id
            )

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        async with self._session_factory() as session:
            try:
                result = getattr(SettingsService(session), name)(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise

    def __getattr__(self, name: str) -> Any:
        async def invoke(*args: Any, **kwargs: Any) -> Any:
            return await self._call(name, *args, **kwargs)

        return invoke


class _TaskScopedCancellationService:
    """Translate graph-facing cancellation checks to the internal task ID."""

    def __init__(self, delegate: Any, task_internal_id: int) -> None:
        self._delegate = delegate
        self._task_internal_id = task_internal_id

    async def is_cancelled(self, *, task_id: str | None = None) -> bool:
        return await self._delegate.is_cancelled(task_id=str(self._task_internal_id))

    async def raise_if_cancelled(self, *, task_id: str | None = None) -> None:
        await self._delegate.raise_if_cancelled(task_id=str(self._task_internal_id))


class ProductionRuntimeContextFactory:
    """Create a fresh, non-serializable RuntimeContext for one graph invoke."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        event_bus_provider: Callable[[], Any],
        cancellation_service: Any | None = None,
        tool_executor: Any | None = None,
        clock: Callable[[], datetime] = datetime.utcnow,
        context_engine: Any | None = None,
        context_llm_invoker: Any | None = None,
        tool_output_manager: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._event_bus_provider = event_bus_provider
        self._cancellation_service = cancellation_service
        self._tool_executor = tool_executor or ToolExecutor(tool_registry)
        self._clock = clock
        # CE-02 WP-8 / CE-04 WP-9：Context Engine 与统一 Invoker 可选注入。
        # 默认 None → CE-02/CE-04 能力在 flag 关闭时不进入调用链。
        self._context_engine = context_engine
        self._context_llm_invoker = context_llm_invoker
        self._tool_output_manager = tool_output_manager or ToolOutputManager(
            PayloadStorageService(
                ContextPayloadRepository,
                backend=build_payload_backend("oss"),
                storage_backend="oss",
            ),
            tool_call_repository_factory=ToolCallRepository,
        )

    async def __call__(self, state: Dict[str, Any]) -> RuntimeContext:
        task_public_id = str(state.get("task_id") or "").strip()
        if not task_public_id:
            raise ValueError("RuntimeContext requires a non-empty public task_id")

        task_internal_id = self._required_identity(state, "task_internal_id")
        conversation_internal_id = self._required_identity(
            state, "conversation_internal_id"
        )
        user_internal_id = self._required_identity(state, "user_internal_id")

        # CE-05 WP-2: 任务路径加载 frozen manifest → TaskScopedFeatureFlagResolver。
        # 从 agent_tasks.task_context_json.frozen_flags_manifest 读取；缺失 →
        # 旧任务回填兼容 profile（Resolver 内部处理，不读动态 env）。
        task_flag_resolver = await self._load_task_flag_resolver(
            task_internal_id=task_internal_id,
            state=state,
        )
        bus = self._event_bus_provider()
        if bus is None:
            raise RuntimeError("RuntimeContext requires an initialized live event bus")
        cancellation_service = self._cancellation_service or DistributedCancellationService(
            session_factory=self._session_factory,
            bus=bus,
            clock=self._clock,
        )

        sink = LiveAgentEventSink(
            session_factory=self._session_factory,
            event_repo=_SessionScopedEventRepository(self._session_factory),
            allocator=SequenceNumberAllocator(session_factory=self._session_factory),
            bus=bus,
            task_internal_id=task_internal_id,
            task_public_id=task_public_id,
            conversation_internal_id=conversation_internal_id,
            user_internal_id=user_internal_id,
            graph_run_id=str(state.get("graph_run_id") or f"run-{task_public_id}"),
            graph_version=str(state.get("graph_version") or "v2"),
            clock=self._clock,
        )
        settings_service = _SessionScopedSettingsService(
            self._session_factory, user_internal_id
        )
        tool_adapter = TestAgentToolAdapter(
            tool_executor=self._tool_executor,
            event_sink=sink,
            session_factory=self._session_factory,
            clock=self._clock,
            tool_call_recorder=ProductionToolOutputRecorder(
                session_factory=self._session_factory,
                manager=self._tool_output_manager,
                task_flag_resolver=task_flag_resolver,
                user_internal_id=user_internal_id,
                conversation_internal_id=conversation_internal_id,
                task_internal_id=task_internal_id,
                task_public_id=task_public_id,
                clock=self._clock,
            ),
        )
        # Phase 2.9B.1: 动态子图(Preparation/Repair/Incremental)的决策主模型。
        # LLMClient 不可序列化 → 绝不写入 Checkpoint State;每次 graph invoke
        # 都通过 context_factory 重新构造。config_provider 按用户解析,
        # 未配置时 LLMClient 内部会抛 LLMNotConfigured,由子图 fallback 处理。
        llm_client = await _build_llm_client_for_user(settings_service, user_internal_id)
        return RuntimeContext(
            user_internal_id=user_internal_id,
            task_internal_id=task_internal_id,
            conversation_internal_id=conversation_internal_id,
            session_factory=self._session_factory,
            settings_service=settings_service,
            event_sink=sink,
            cancellation_service=_TaskScopedCancellationService(
                cancellation_service, task_internal_id
            ),
            clock=self._clock,
            tool_adapter=tool_adapter,
            llm_client=llm_client,
            context_engine=self._context_engine,
            context_llm_invoker=self._context_llm_invoker,
            task_flag_resolver=task_flag_resolver,
            task_public_id=task_public_id,
            conversation_public_id=(
                str(state.get("conversation_public_id") or "").strip() or None
            ),
            project_context=(
                dict(state["project_context"])
                if isinstance(state.get("project_context"), dict)
                else None
            ),
        )

    async def _load_task_flag_resolver(
        self,
        *,
        task_internal_id: int,
        state: Dict[str, Any],
    ):
        """从 agent_tasks.task_context_json.frozen_flags_manifest 加载 Resolver。

        缺失 Manifest → 历史任务使用固定兼容 Profile（不读动态 env）。
        加载失败 → fail closed；任务路径绝不能退回进程级配置。
        """
        try:
            from sqlalchemy import select
            from app.models.agent_task import AgentTask
            from app.context_engine.freeze.runtime import resolve_task_resolver
            from app.context_engine.freeze.service import extract_manifest

            task_engine_type = str(state.get("engine_type") or "").strip() or None
            async with self._session_factory() as session:
                row = (
                    await session.execute(
                        select(AgentTask.task_context_json, AgentTask.engine_type).where(
                            AgentTask.id == task_internal_id
                        )
                    )
                ).one_or_none()
            if row is None:
                raise RuntimeError("task row not found")
            ctx_json = row[0]
            engine_type = task_engine_type or row[1]
            manifest = extract_manifest(ctx_json)
            if manifest is not None:
                return await resolve_task_resolver(engine_type, ctx_json)
            # 旧任务无 Manifest → 固定兼容 profile（旧任务回填由调用方/回填流程负责）
            from app.context_engine.freeze.runtime import build_backfill_resolver
            return build_backfill_resolver(engine_type)
        except Exception as exc:  # noqa: BLE001 — identity/config boundary
            logger.error(
                "ProductionRuntimeContextFactory load task_flag_resolver 失败 | task=%s | err=%s",
                task_internal_id, exc,
            )
            raise RuntimeError(
                f"task flag resolver unavailable for task {task_internal_id}"
            ) from exc

    @staticmethod
    def _required_identity(state: Dict[str, Any], name: str) -> int:
        value = state.get(name)
        try:
            identity = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"RuntimeContext requires {name}") from exc
        if identity <= 0:
            raise ValueError(f"RuntimeContext requires {name}")
        return identity


__all__ = ["ProductionRuntimeContextFactory"]


# 模块定位:Production-only RuntimeContext 构造(Phase 2.8R-D)
#
# 模块分工:
#   graph checkpoint 只存 serializable state,不存任何 live collaborator;
#   本模块重建 resume 时的 live 依赖:AsyncSession / event_sink /
#   tool_adapter / settings_service / cancellation_service。
#
# 链路:
#   checkpoint restore → coordinator.ainvoke(graph, thread_id)
#     → factory.rebuild(thread_id)
#       → RuntimeContext(全部字段, frozen + slots)
#
# 关键约束:
#   - 重建必须 idempotent(同一 thread_id 多次 rebuild 字段一致);
#   - 没 DB 连接 → raise,不要静默兜底;
#   - 走 ContextEngine 链路时,本模块与 context_runtime_builder 共享
#     同一套构造顺序,不可交叉替换。
