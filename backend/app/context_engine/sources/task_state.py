"""Task State Source Adapter：TaskStateRef 视图（不 dump 全 State）。

CE-02 WP-2：映射 agent_tasks 关键字段 + 锁定章节 + 当前 Node。
owner-scope：task 必须属于当前 user。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.context_engine.models.context import ContextItem, ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind, ContextTrust, SourceType
from app.context_engine.models.source import ContextWarning, LockedSection, SourceCollectResult
from app.context_engine.sources.registry import ContextSourceAdapterProtocol


class TaskStateSourceAdapter:
    """任务状态视图：agent_tasks 字段 + plan + 锁定章节。

    不 dump 全量 LangGraph State；只取与 LLM 调用相关的精简视图。
    """

    source_kind = ContextKind.TASK_STATE

    def __init__(self, *, token_counter=None) -> None:
        self._token_counter = token_counter

    async def collect(
        self,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        *,
        runtime_context,
    ) -> SourceCollectResult:
        started = _now_ms()
        task_id = request.task_id
        user_id = request.user_id
        if not task_id:
            return SourceCollectResult(
                adapter_key="task_state",
                kind=self.source_kind,
                attempted=False,
                degraded=True,
                failure_code="context.source.no_task",
                warnings=[
                    ContextWarning(
                        code="context.source.no_task",
                        detail="task_id 缺失，跳过任务状态",
                        adapter_key="task_state",
                    )
                ],
                latency_ms=_now_ms() - started,
            )

        warnings: list[ContextWarning] = []
        locked_sections: list[LockedSection] = []
        try:
            async with runtime_context.session_factory() as session:
                from app.context_engine.sources._helpers import user_internal_id
                from app.repositories.agent_task_repository import AgentTaskRepository

                repo = AgentTaskRepository(session)
                task = await repo.get_owned_task(task_id, user_internal_id(runtime_context, request))

                if task is None:
                    return SourceCollectResult(
                        adapter_key="task_state",
                        kind=self.source_kind,
                        attempted=False,
                        degraded=True,
                        failure_code="context.source.task_not_found",
                        warnings=[
                            ContextWarning(
                                code="context.source.task_not_found",
                                detail="任务不存在或不属于当前用户",
                                adapter_key="task_state",
                            )
                        ],
                        latency_ms=_now_ms() - started,
                    )

                plan = task.plan_json or {}
                task_context = task.task_context_json or {}
                section_confirm_config = task_context.get("section_confirm_config") or plan.get("section_confirm_config")
                locked_ids = _derive_locked_ids(section_confirm_config)

                content_parts: list[str] = []
                if task.title:
                    content_parts.append(f"任务标题: {task.title}")
                if task.user_instruction:
                    content_parts.append(f"任务指令: {task.user_instruction}")
                content_parts.append(f"状态: {task.status} 当前节点: {task.current_node or 'unknown'}")
                content_parts.append(f"任务类型: {task.task_type}")
                if locked_ids:
                    content_parts.append(f"锁定章节: {', '.join(locked_ids)}")

                state_ref_block = _render_state_ref(request.state_ref)
                if state_ref_block:
                    content_parts.append(state_ref_block)

                for sid in locked_ids:
                    locked_sections.append(
                        LockedSection(
                            section_id=sid,
                            locked=True,
                            authority="task_state",
                            version="v1",
                            content_mode="full_text",
                        )
                    )

                items: list[ContextItem] = []
                if content_parts:
                    rendered = "\n".join(content_parts)
                    items.append(
                        ContextItem(
                            item_id=f"task:{task.public_id}",
                            kind=ContextKind.TASK_STATE,
                            source_type=SourceType.TASK_STATE,
                            source_ref=task.public_id,
                            title="task_state",
                            content=rendered,
                            authority=70,
                            priority=10,
                            estimated_tokens=self._estimate(rendered),
                            trust=ContextTrust.BUSINESS_EVIDENCE,
                            metadata={
                                "status": task.status,
                                "current_node": task.current_node,
                                "locked_section_ids": locked_ids,
                                "task_type": task.task_type,
                            },
                        )
                    )
        except Exception as exc:  # noqa: BLE001 — adapter 捕获后降级
            warnings.append(
                ContextWarning(
                    code="context.source.task_state_error",
                    detail="任务状态收集失败，已降级",
                    adapter_key="task_state",
                )
            )
            return SourceCollectResult(
                adapter_key="task_state",
                kind=self.source_kind,
                items=[],
                warnings=warnings,
                attempted=True,
                degraded=True,
                failure_code="context.source.task_state_error",
                latency_ms=_now_ms() - started,
            )

        return SourceCollectResult(
            adapter_key="task_state",
            kind=self.source_kind,
            items=items,
            warnings=warnings,
            attempted=True,
            degraded=False,
            latency_ms=_now_ms() - started,
            failure_code=None,
        )

    def _estimate(self, text: str) -> int:
        if self._token_counter is not None:
            return self._token_counter.estimate(text).tokens
        return max(1, len(text) // 3)


def _derive_locked_ids(section_confirm_config) -> list[str]:
    """从 section_confirm_config.sections[].suggested_action == keep_template 派生。"""
    if not section_confirm_config:
        return []
    sections = section_confirm_config.get("sections") or []
    locked: list[str] = []
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        if sec.get("suggested_action") == "keep_template":
            sid = sec.get("section_id")
            if isinstance(sid, str) and sid:
                locked.append(sid)
    return locked


def _render_state_ref(state_ref) -> str:
    """Render explicit LLM-call state into the TaskState section."""
    if not isinstance(state_ref, dict) or not state_ref:
        return ""

    parts: list[str] = []
    instruction = str(state_ref.get("test_plan_generation_instruction") or "").strip()
    if instruction:
        parts.append(f"test_plan_generation_instruction: {instruction}")

    ai_fields = state_ref.get("ai_fields")
    if isinstance(ai_fields, list) and ai_fields:
        lines = ["ai_fields:"]
        for item in ai_fields[:80]:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "").strip()
            title = str(item.get("title") or "").strip()
            section_id = str(item.get("section_id") or "").strip()
            level = item.get("level")
            schemas = item.get("table_schemas")
            schema_hint = ""
            if isinstance(schemas, list) and schemas:
                headers: list[str] = []
                for schema in schemas[:3]:
                    if isinstance(schema, dict) and isinstance(schema.get("headers"), list):
                        headers.extend(str(h) for h in schema.get("headers")[:20])
                if headers:
                    schema_hint = " | table_headers=" + ", ".join(headers[:40])
            lines.append(
                f"- field={field} title={title} section_id={section_id} level={level}{schema_hint}"
            )
        parts.append("\n".join(lines))

    for key in ("template_headings", "table_fields"):
        values = state_ref.get(key)
        if isinstance(values, list) and values:
            parts.append(
                f"{key}:\n"
                + "\n".join(f"- {str(v)[:300]}" for v in values[:120])
            )

    if not parts:
        return ""
    return "explicit_state_ref:\n" + "\n".join(parts)[:12000]


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
# auto-appended module-level note: task_state source。
