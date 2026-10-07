"""Artifact Source Adapter：旧 Artifact / 当前批次 / Review 对象。

CE-02 WP-2 / WP-3b：锁定章节按 Profile / Required Anchor Policy 决定
full text / authoritative summary / hash+ref（**不统一只给摘要**）。
owner-scope：task 必须属于当前 user。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.context_engine.models.context import ContextItem, ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind, ContextTrust, SourceType
from app.context_engine.models.source import ContextWarning, LockedSection, SourceCollectResult
from app.context_engine.sources.registry import ContextSourceAdapterProtocol


class ArtifactSourceAdapter:
    """Artifact 来源：按 task 收集旧 Artifact 的章节元数据。

    正文内容（generated_content / 章节文本）由 Payload 或解析器提供；
    adapter 提供引用 + 章节结构 + 锁定章节元数据。
    """

    source_kind = ContextKind.EVIDENCE

    def __init__(
        self,
        *,
        token_counter=None,
        max_metadata_chars: int = 3000,
        include_section_text: bool = True,
    ) -> None:
        self._token_counter = token_counter
        self._max_metadata_chars = max_metadata_chars
        self._include_section_text = include_section_text

    async def collect(
        self,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        *,
        runtime_context,
    ) -> SourceCollectResult:
        started = _now_ms()
        if request.call_site == "document.qa":
            return SourceCollectResult(
                adapter_key="artifact",
                kind=self.source_kind,
                attempted=False,
                latency_ms=_now_ms() - started,
            )
        task_id = request.task_id
        user_id = request.user_id
        if not task_id:
            return SourceCollectResult(
                adapter_key="artifact",
                kind=self.source_kind,
                attempted=False,
                degraded=True,
                failure_code="context.source.no_task",
                warnings=[
                    ContextWarning(
                        code="context.source.no_task",
                        detail="task_id 缺失，跳过 Artifact 来源",
                        adapter_key="artifact",
                    )
                ],
                latency_ms=_now_ms() - started,
            )

        items: list[ContextItem] = []
        warnings: list[ContextWarning] = []
        locked_sections: list[LockedSection] = []
        try:
            async with runtime_context.session_factory() as session:
                from app.context_engine.sources._helpers import user_internal_id
                from app.repositories.artifact_repository import ArtifactRepository

                internal_user_id = user_internal_id(runtime_context, request)
                task_internal_id = await _resolve_task_internal_id(
                    session=session,
                    task_id=task_id,
                    user_id=internal_user_id,
                )
                if task_internal_id is None:
                    return SourceCollectResult(
                        adapter_key="artifact",
                        kind=self.source_kind,
                        items=[],
                        warnings=warnings,
                        attempted=True,
                        degraded=False,
                        latency_ms=_now_ms() - started,
                    )
                repo = ArtifactRepository(session)
                artifacts = await repo.list_by_task(internal_user_id, task_internal_id)
                for art in artifacts:
                    if art.deleted_at is not None:
                        continue
                    meta = art.metadata_json or {}
                    section_ids = meta.get("section_ids") or []
                    locked_ids = [
                        s for s in section_ids
                        if _is_locked_section(s, meta)
                    ]
                    for sid in locked_ids:
                        locked_sections.append(
                            LockedSection(
                                section_id=str(sid),
                                locked=True,
                                authority="artifact",
                                version=f"v{art.version_no}",
                                hash=art.input_hash or art.file_hash,
                                content_mode="full_text",
                            )
                        )
                    rendered = (
                        f"Artifact: {art.file_name}（{art.artifact_type}）\n"
                        f"版本: v{art.version_no} 状态: {art.status}\n"
                        f"哈希: {art.input_hash or art.file_hash or 'n/a'}\n"
                        f"章节: {', '.join(str(s) for s in section_ids) or 'n/a'}"
                    )
                    if locked_ids:
                        rendered += f"\n锁定章节: {', '.join(str(s) for s in locked_ids)}"
                    items.append(
                        ContextItem(
                            item_id=f"artifact:{art.public_id}",
                            kind=ContextKind.EVIDENCE,
                            source_type=SourceType.ARTIFACT,
                            source_ref=art.public_id,
                            title=art.file_name,
                            content=rendered[: self._max_metadata_chars],
                            authority=75,
                            priority=8,
                            estimated_tokens=self._estimate(rendered[: self._max_metadata_chars]),
                            trust=ContextTrust.BUSINESS_EVIDENCE,
                            metadata={
                                "artifact_type": art.artifact_type,
                                "version_no": art.version_no,
                                "status": art.status,
                                "section_ids": section_ids,
                                "locked_section_ids": locked_ids,
                                "hash": art.input_hash or art.file_hash,
                            },
                        )
                    )
        except Exception as exc:  # noqa: BLE001 — adapter 捕获后降级
            warnings.append(
                ContextWarning(
                    code="context.source.artifact_error",
                    detail="Artifact 来源收集失败，已降级",
                    adapter_key="artifact",
                )
            )
            return SourceCollectResult(
                adapter_key="artifact",
                kind=self.source_kind,
                items=[],
                warnings=warnings,
                attempted=True,
                degraded=True,
                failure_code="context.source.artifact_error",
                latency_ms=_now_ms() - started,
            )

        return SourceCollectResult(
            adapter_key="artifact",
            kind=self.source_kind,
            items=items,
            warnings=warnings,
            attempted=True,
            degraded=False,
            latency_ms=_now_ms() - started,
        )

    def _estimate(self, text: str) -> int:
        if self._token_counter is not None:
            return self._token_counter.estimate(text).tokens
        return max(1, len(text) // 3)


def _is_locked_section(section_id, meta: dict) -> bool:
    """判断章节是否锁定（suggested_action == keep_template 或显式 locked）。"""
    if meta.get("locked_sections"):
        return str(section_id) in {str(s) for s in meta.get("locked_sections", [])}
    return False


async def _resolve_task_internal_id(*, session, task_id: str, user_id: int) -> int | None:
    if str(task_id).isdigit():
        return int(task_id)
    from app.repositories.agent_task_repository import AgentTaskRepository

    task = await AgentTaskRepository(session).get_owned_task(str(task_id), user_id)
    if task is None:
        return None
    return int(task.id)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
# auto-appended module-level note: Artifact 类型 source adapter。
