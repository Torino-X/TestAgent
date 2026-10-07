"""SourceOrchestrator — 收集编排：Required 顺序 + Optional 并发。

CE-02 WP-2：
- Required 顺序收集（失败 fail/clarify）；
- Optional 并发收集（受限并发 + 独立 session + 统一超时，失败 warning/degrade）；
- 结果按 Section ID 聚合。
- **取消语义**：Source 收集阶段取消时**尚未 begin**，不创建或 abandon
  Snapshot（仅记录安全 Trace，不调 LLM）；已创建 Snapshot 后的取消由
  WP-7/WP-9 统一处理。
- ``deadline exhausted``（预算耗尽）与 ``cancel`` / ``timeout`` /
  ``degraded`` / ``required_failure`` 各自独立分类。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.context_engine.errors import ContextEngineStage, raise_engine_error
from app.context_engine.models.context import ContextItem, ContextRequest, ContextPlan, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind
from app.context_engine.models.source import ContextWarning, LockedSection, SourceCollectResult
from app.context_engine.sources.deadline import Deadline, DeadlineExceeded
from app.context_engine.sources.registry import SourceAdapterRegistry


# Source adapters can perform network or storage I/O.  A bounded default keeps a
# failed optional source from leaving the whole agent run in ``running`` forever.
_DEFAULT_SOURCE_DEADLINE_MS = 60_000


@dataclass(frozen=True)
class SourceCollectionOutcome:
    """收集结果：按 Section ID 聚合 + 独立分类。"""

    by_section: dict[str, list[ContextItem]] = field(default_factory=dict)
    warnings: list[ContextWarning] = field(default_factory=list)
    locked_sections: list[LockedSection] = field(default_factory=list)
    attempted: bool = True
    degraded: bool = False
    failure_code: str | None = None
    latency_ms: int = 0
    retrieval_run_ids: list[str] = field(default_factory=list)
    # 分类（独立传播，互斥）
    cancelled: bool = False
    timed_out: bool = False
    deadline_exhausted: bool = False
    required_failure: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure_code is None and not self.required_failure

    def all_items(self) -> list[ContextItem]:
        return [item for section_items in self.by_section.values() for item in section_items]


class SourceOrchestrator:
    """收集编排器：驱动 registry 的 adapter，按 Section 聚合。"""

    def __init__(
        self,
        registry: SourceAdapterRegistry,
        *,
        concurrency: int = 4,
        deadline_ms: int | None = _DEFAULT_SOURCE_DEADLINE_MS,
        token_counter=None,
    ) -> None:
        self._registry = registry
        self._concurrency = max(1, concurrency)
        self._deadline_ms = deadline_ms
        self._token_counter = token_counter

    async def collect(
        self,
        request: ContextRequest,
        plan: ContextPlan,
        scope: ContextScope,
        *,
        runtime_context,
    ) -> SourceCollectionOutcome:
        started = _now_ms()
        deadline = Deadline(deadline_ms=self._deadline_ms, start_epoch_ms=started)

        # 取消检查（begin 前取消：不创建 Snapshot，只安全传播）
        if _is_cancelled(runtime_context, request.task_id):
            return SourceCollectionOutcome(
                attempted=False,
                cancelled=True,
                failure_code="context.source.cancelled",
                latency_ms=_now_ms() - started,
            )

        required_outcome = await self._collect_required(
            request, plan, scope, runtime_context, deadline
        )
        if required_outcome.required_failure:
            return required_outcome

        optional_outcome = await self._collect_optional(
            request, plan, scope, runtime_context, deadline
        )

        # 合并
        by_section: dict[str, list[ContextItem]] = dict(required_outcome.by_section)
        for section_id, items in optional_outcome.by_section.items():
            by_section.setdefault(section_id, []).extend(items)

        warnings = required_outcome.warnings + optional_outcome.warnings
        locked_sections = required_outcome.locked_sections + optional_outcome.locked_sections
        retrieval_run_ids = (
            required_outcome.retrieval_run_ids + optional_outcome.retrieval_run_ids
        )

        return SourceCollectionOutcome(
            by_section=by_section,
            warnings=warnings,
            locked_sections=locked_sections,
            attempted=True,
            degraded=required_outcome.degraded or optional_outcome.degraded,
            cancelled=optional_outcome.cancelled,
            timed_out=optional_outcome.timed_out,
            deadline_exhausted=optional_outcome.deadline_exhausted,
            latency_ms=_now_ms() - started,
            retrieval_run_ids=retrieval_run_ids,
        )

    # ── Required：顺序收集，失败 fail/clarify ────────────────────────

    async def _collect_required(
        self,
        request: ContextRequest,
        plan: ContextPlan,
        scope: ContextScope,
        runtime_context,
        deadline: Deadline,
    ) -> SourceCollectionOutcome:
        started = _now_ms()
        by_section: dict[str, list[ContextItem]] = {}
        warnings: list[ContextWarning] = []
        locked_sections: list[LockedSection] = []
        retrieval_run_ids: list[str] = []

        for section_id, section_plan in plan.section_plans.items():
            if not section_plan.required:
                continue
            result = await self._collect_section(
                request, section_plan, scope, runtime_context, deadline, section_id
            )
            if result.failure_code:
                return SourceCollectionOutcome(
                    attempted=True,
                    required_failure=result.failure_code,
                    failure_code=result.failure_code,
                    warnings=warnings + result.warnings,
                    latency_ms=_now_ms() - started,
                    retrieval_run_ids=[*retrieval_run_ids, *result.retrieval_run_ids],
                )
            by_section[section_id] = result.items
            warnings.extend(result.warnings)
            locked_sections.extend(result.locked_sections)
            retrieval_run_ids.extend(result.retrieval_run_ids)

        return SourceCollectionOutcome(
            by_section=by_section,
            warnings=warnings,
            locked_sections=locked_sections,
            attempted=True,
            degraded=bool(warnings),
            latency_ms=_now_ms() - started,
            retrieval_run_ids=retrieval_run_ids,
        )

    # ── Optional：并发收集，失败 warning/degrade ──────────────────────

    async def _collect_optional(
        self,
        request: ContextRequest,
        plan: ContextPlan,
        scope: ContextScope,
        runtime_context,
        deadline: Deadline,
    ) -> SourceCollectionOutcome:
        started = _now_ms()
        optional: list[tuple[str, SectionPlan]] = [
            (section_id, sp)
            for section_id, sp in plan.section_plans.items()
            if not sp.required
        ]
        by_section: dict[str, list[ContextItem]] = {}
        warnings: list[ContextWarning] = []
        locked_sections: list[LockedSection] = []
        retrieval_run_ids: list[str] = []
        cancelled = False
        timed_out = False
        deadline_exhausted = False

        sem = asyncio.Semaphore(self._concurrency)

        async def _run(section_id: str, section_plan: SectionPlan) -> SourceCollectResult:
            async with sem:
                return await self._collect_section(
                    request, section_plan, scope, runtime_context, deadline, section_id
                )

        tasks = [_run(section_id, sp) for section_id, sp in optional]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, BaseException):
                warnings.append(
                    ContextWarning(
                        code="context.source.optional_collect_error",
                        detail="可选来源收集异常，已降级",
                    )
                )
                continue
            if result is None:
                continue
            if result.failure_code == "context.source.cancelled":
                cancelled = True
            elif result.failure_code == "context.source.deadline_exhausted":
                deadline_exhausted = True
            elif result.failure_code == "context.source.timeout":
                timed_out = True
            elif result.failure_code:
                warnings.append(
                    ContextWarning(
                        code=result.failure_code,
                        detail="可选来源收集失败，已降级",
                        adapter_key=result.adapter_key,
                    )
                )
            by_section.setdefault(result.kind.value, []).extend(result.items)
            warnings.extend(result.warnings)
            locked_sections.extend(result.locked_sections)
            retrieval_run_ids.extend(result.retrieval_run_ids)

        return SourceCollectionOutcome(
            by_section=by_section,
            warnings=warnings,
            locked_sections=locked_sections,
            attempted=True,
            degraded=bool(warnings) or deadline_exhausted or timed_out,
            cancelled=cancelled,
            timed_out=timed_out,
            deadline_exhausted=deadline_exhausted,
            latency_ms=_now_ms() - started,
            retrieval_run_ids=retrieval_run_ids,
        )

    # ── 单 Section 收集（含 deadline / 取消检查）────────────────────────

    async def _collect_section(
        self,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        runtime_context,
        deadline: Deadline,
        section_id: str,
    ) -> SourceCollectResult:
        try:
            deadline.raise_if_expired(_now_ms())
        except DeadlineExceeded:
            return SourceCollectResult(
                adapter_key=section_id,
                kind=section_plan.kind,
                attempted=False,
                degraded=True,
                failure_code="context.source.deadline_exhausted",
                latency_ms=0,
            )

        if _is_cancelled(runtime_context, request.task_id):
            return SourceCollectResult(
                adapter_key=section_id,
                kind=section_plan.kind,
                attempted=False,
                degraded=True,
                failure_code="context.source.cancelled",
                latency_ms=0,
            )

        adapters = self._registry.all_for_kind(section_plan.kind)
        if not adapters:
            return SourceCollectResult(
                adapter_key=section_id,
                kind=section_plan.kind,
                attempted=False,
                degraded=True,
                failure_code="context.source.adapter_not_found",
                latency_ms=0,
            )

        all_items: list[ContextItem] = []
        warnings: list[ContextWarning] = []
        locked_sections: list[LockedSection] = []
        retrieval_run_ids: list[str] = []
        started = _now_ms()
        first_failure: str | None = None
        for adapter in adapters:
            try:
                result = await self._collect_adapter_with_deadline(
                    adapter=adapter,
                    request=request,
                    section_plan=section_plan,
                    scope=scope,
                    runtime_context=runtime_context,
                    deadline=deadline,
                    section_id=section_id,
                    started=started,
                )
            except Exception as exc:  # noqa: BLE001 — adapter 捕获后降级
                warnings.append(
                    ContextWarning(
                        code="context.source.adapter_error",
                        detail="SourceAdapter 收集异常，已降级",
                        adapter_key=getattr(adapter, "source_kind", None).value
                        if getattr(adapter, "source_kind", None)
                        else None,
                    )
                )
                if first_failure is None:
                    first_failure = "context.source.adapter_error"
                continue
            if result.failure_code == "context.source.cancelled":
                return result
            if result.failure_code and first_failure is None:
                first_failure = result.failure_code
            all_items.extend(result.items)
            warnings.extend(result.warnings)
            locked_sections.extend(result.locked_sections)
            retrieval_run_ids.extend(result.retrieval_run_ids)

        # 聚合为单 Section 结果（携带首个 adapter failure_code，供 Required fail-fast）
        return SourceCollectResult(
            adapter_key=section_id,
            kind=section_plan.kind,
            items=all_items,
            warnings=warnings,
            locked_sections=locked_sections,
            attempted=True,
            degraded=bool(warnings) or first_failure is not None,
            failure_code=first_failure,
            latency_ms=_now_ms() - started,
            retrieval_run_ids=retrieval_run_ids,
        )

    async def _collect_adapter_with_deadline(
        self,
        *,
        adapter: Any,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        runtime_context: Any,
        deadline: Deadline,
        section_id: str,
        started: int,
    ) -> SourceCollectResult:
        """Collect one adapter without allowing it to outlive the source budget."""
        remaining_ms = deadline.remaining_ms(_now_ms())
        if deadline.deadline_ms is None:
            return await adapter.collect(
                request, section_plan, scope, runtime_context=runtime_context
            )
        if remaining_ms <= 0:
            return self._timeout_result(section_id, section_plan.kind, started, adapter)

        collect_task = asyncio.create_task(
            adapter.collect(request, section_plan, scope, runtime_context=runtime_context)
        )
        try:
            done, _ = await asyncio.wait(
                {collect_task}, timeout=remaining_ms / 1000
            )
        except asyncio.CancelledError:
            collect_task.cancel()
            collect_task.add_done_callback(_consume_adapter_task_result)
            raise

        if collect_task in done:
            return collect_task.result()

        collect_task.cancel()
        collect_task.add_done_callback(_consume_adapter_task_result)
        return self._timeout_result(section_id, section_plan.kind, started, adapter)

    @staticmethod
    def _timeout_result(
        section_id: str,
        kind: ContextKind,
        started: int,
        adapter: Any,
    ) -> SourceCollectResult:
        source_kind = getattr(adapter, "source_kind", None)
        source_kind_value = (
            source_kind.value if isinstance(source_kind, ContextKind) else None
        )
        return SourceCollectResult(
            adapter_key=section_id,
            kind=kind,
            warnings=[
                ContextWarning(
                    code="context.source.timeout",
                    detail="Source adapter collection timed out and was degraded",
                    adapter_key=source_kind_value,
                    source_kind=source_kind_value,
                )
            ],
            attempted=True,
            degraded=True,
            failure_code="context.source.timeout",
            latency_ms=_now_ms() - started,
        )


def _is_cancelled(runtime_context, task_id: str | None) -> bool:
    cancellation_service = getattr(runtime_context, "cancellation_service", None)
    if cancellation_service is None or task_id is None:
        return False
    try:
        return bool(cancellation_service.is_cancelled(task_id))
    except Exception:  # noqa: BLE001
        return False


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _consume_adapter_task_result(task: asyncio.Task[Any]) -> None:
    """Consume a cancelled adapter task so a late adapter exception is not leaked."""
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass
# auto-appended module-level note: sources 编排层。
