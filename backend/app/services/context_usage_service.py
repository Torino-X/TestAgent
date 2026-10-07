"""ContextUsageService — 当前 Conversation 的 Context Usage 聚合。

WP-BE-02：Context Usage 的正式定义是"当前 Conversation 最近一次成功 Context
Assembly 实际选择进入模型输入上下文的规模"，**不是**所有历史消息/文件/Memory
的总和。

数据来源（优先）：
- ``llm_context_snapshots``（最近一次 status='completed' 且 context_kind='active'）
  - ``estimated_input_tokens`` → used_tokens
  - ``section_stats_json`` → 5 分类聚合（per-section kind → UI 分类）
  - ``context_window_tokens`` / ``model_name_snapshot`` → model
- ``model_configs.context_window_tokens``（用户显式配置，优先于内置 registry）

5 分类映射（用户可理解，不暴露内部 ContextKind）：
    conversation_history  : CONVERSATION
    project_documents     : KNOWLEDGE
    task_context          : CURRENT_GOAL + TASK_STATE + EVIDENCE
    user_memory           : MEMORY
    system_instructions   : SYSTEM_RULES + PROJECT_INSTRUCTIONS + CALL_CONTRACT

规则：
- 未知模型 context_window_tokens=None → percent=None（禁止假设 128K/200K）。
- 无 snapshot → 返回可用状态 unknown（不伪造 0%）。
- raw percent 可 >100 → over_limit=true。
- Compaction recommended 基于 ContextBudget / Preflight soft-threshold 派生，
  不硬编码 70%/80%/90%。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context_engine.models.enums import ContextKind
from app.services.conversation_context_service import ConversationContextService

logger = logging.getLogger(__name__)

_PREVIEW_RETRIEVAL_QUERY_CHARS = 2000


def _bounded_preview_query(value: str) -> str:
    """Keep idle retrieval focused without exporting an entire long turn."""
    text = (value or "").strip()
    if len(text) <= _PREVIEW_RETRIEVAL_QUERY_CHARS:
        return text
    marker = "\n...[retrieval focus shortened]...\n"
    head_chars = 1200
    tail_chars = _PREVIEW_RETRIEVAL_QUERY_CHARS - head_chars - len(marker)
    return f"{text[:head_chars]}{marker}{text[-tail_chars:]}"


def _preview_session_factory():
    """Open an independent read-only-preview session for source adapters."""
    from app.db.session import AsyncSessionLocal

    return AsyncSessionLocal()

# UI 5 分类 → ContextKind 映射（固定）
UI_CATEGORIES: tuple[str, ...] = (
    "conversation_history",
    "project_documents",
    "task_context",
    "user_memory",
    "system_instructions",
)

_KIND_TO_UI: dict[ContextKind, str] = {
    ContextKind.CONVERSATION: "conversation_history",
    ContextKind.KNOWLEDGE: "project_documents",
    ContextKind.CURRENT_GOAL: "task_context",
    ContextKind.TASK_STATE: "task_context",
    ContextKind.EVIDENCE: "task_context",
    ContextKind.MEMORY: "user_memory",
    ContextKind.SYSTEM_RULES: "system_instructions",
    ContextKind.PROJECT_INSTRUCTIONS: "system_instructions",
    ContextKind.CALL_CONTRACT: "system_instructions",
}

# These snapshots audit background/diagnostic work.  They are valuable in the
# database, but they are not the context that produced the latest user-visible
# reply and therefore must never replace the conversation usage card.
_BACKGROUND_CALL_SITE_PATTERNS: tuple[str, ...] = (
    "memory.extract.%",
    "intent.%",
    "compression.%",
    "context.retrieval.%",
    "conversation.title",
    "word_export.%",
    "ce.pilot.%",
    "dynamic_agent.planner",
)


class UnknownContextKindError(ValueError):
    """未映射的 ContextKind：不允许 silent drop（WP-BE-02 要求 3）。"""


def map_kind_to_ui_category(kind: str | ContextKind) -> str:
    """ContextKind → UI 分类。未知 kind 显式抛错（不允许 silent drop）。"""
    if isinstance(kind, str):
        try:
            kind = ContextKind(kind)
        except ValueError as exc:
            raise UnknownContextKindError(
                f"未映射的 ContextKind: {kind!r}"
            ) from exc
    category = _KIND_TO_UI.get(kind)
    if category is None:
        raise UnknownContextKindError(f"未映射的 ContextKind: {kind!r}")
    return category


def aggregate_section_stats(section_stats: dict[str, Any] | None) -> dict[str, int]:
    """把 snapshot.section_stats_json 聚合成 UI 5 分类 token 值。

    每个 section_stats 条目含 ``kind`` 与 ``estimated_tokens``。未知 kind →
    显式抛 UnknownContextKindError（不 silent drop）。
    """
    breakdown = {cat: 0 for cat in UI_CATEGORIES}
    if not section_stats:
        return breakdown
    for section_id, stats in section_stats.items():
        if not isinstance(stats, dict):
            continue
        kind = stats.get("kind")
        if kind is None:
            continue
        category = map_kind_to_ui_category(str(kind))
        breakdown[category] += int(stats.get("estimated_tokens") or 0)
    return breakdown


def normalize_breakdown_to_used_tokens(
    breakdown: dict[str, int],
    used_tokens: int | None,
) -> dict[str, int]:
    """Keep UI breakdown values on the same visible scale as used_tokens.

    ``section_stats_json`` is estimated before the final prompt is rendered,
    while ``estimated_input_tokens`` is estimated from the final prompt text.
    When those heuristics drift, a single UI category can appear larger than
    the total. Scale the breakdown down proportionally in that case so the
    product surface remains internally consistent.
    """
    normalized = {
        cat: max(0, int(breakdown.get(cat) or 0))
        for cat in UI_CATEGORIES
    }
    if used_tokens is None:
        return normalized

    used = max(0, int(used_tokens))
    total = sum(normalized.values())
    if total <= used:
        return normalized
    if used == 0:
        return {cat: 0 for cat in UI_CATEGORIES}

    scaled: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    for cat in UI_CATEGORIES:
        raw = normalized[cat] * used / total
        whole = int(raw)
        scaled[cat] = whole
        remainders.append((raw - whole, cat))

    remaining = used - sum(scaled.values())
    for _, cat in sorted(remainders, reverse=True)[:remaining]:
        scaled[cat] += 1
    return scaled


def build_evidence_receipt(snapshot: Any) -> dict[str, Any]:
    """Build a safe, user-visible receipt for a completed CE snapshot.

    It contains only routing metadata and stable references.  Prompt text,
    document contents, secrets, and retrieval scores remain server-side.
    """
    raw_refs = getattr(snapshot, "included_refs_json", None)
    refs = raw_refs if isinstance(raw_refs, list) else []
    included: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in refs:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "unknown")[:64]
        source_type = str(raw.get("source_type") or "unknown")[:64]
        reference = str(raw.get("source_ref") or raw.get("item_id") or "")[:160]
        key = (kind, source_type, reference)
        if key in seen:
            continue
        seen.add(key)
        included.append(
            {"kind": kind, "source_type": source_type, "reference": reference}
        )

    raw_dropped = getattr(snapshot, "dropped_refs_json", None)
    return {
        "snapshot_public_id": str(getattr(snapshot, "public_id", "")),
        "call_site": str(getattr(snapshot, "call_site", None) or "unknown"),
        "profile_key": str(getattr(snapshot, "context_profile_key", None) or "unknown"),
        "profile_version": str(getattr(snapshot, "context_profile_version", None) or "unknown"),
        "included_source_count": len(included),
        "dropped_source_count": len(raw_dropped) if isinstance(raw_dropped, list) else 0,
        "included_sources": included[:20],
    }


@dataclass(frozen=True)
class ContextUsageData:
    """Context Usage 响应数据（不含正文/secret）。"""

    conversation_public_id: str
    model: dict[str, Any]
    usage: dict[str, Any]
    breakdown: dict[str, int]
    compaction: dict[str, Any]
    as_of: str | None = None
    available: bool = False
    snapshot_public_id: str | None = None
    debug_details_enabled: bool = False
    evidence_receipt: dict[str, Any] | None = None
    recent_evidence_receipts: list[dict[str, Any]] | None = None
    source: str = "last_completed_snapshot"
    preflight: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_public_id": self.conversation_public_id,
            "model": self.model,
            "usage": self.usage,
            "breakdown": self.breakdown,
            "compaction": self.compaction,
            "as_of": self.as_of,
            "available": self.available,
            "snapshot_public_id": self.snapshot_public_id,
            "debug_details_enabled": self.debug_details_enabled,
            "evidence_receipt": self.evidence_receipt if self.debug_details_enabled else None,
            "recent_evidence_receipts": self.recent_evidence_receipts if self.debug_details_enabled else None,
            "source": self.source,
            "preflight": self.preflight,
        }


class ContextUsageService:
    """聚合最近成功 Snapshot 的 Context Usage（owner-scoped）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── 公开入口 ──────────────────────────────────────────────────

    async def get_context_usage(
        self,
        *,
        conversation_public_id: str,
        user_internal_id: int,
        context_engine=None,
        preview_message: str | None = None,
        preview_attached_file_ids: list[str] | None = None,
        preview_next_request: bool = False,
    ) -> ContextUsageData:
        """返回当前 Conversation 最近一次成功 Snapshot 的 usage。

        - 无 snapshot → available=False，model 仍解析，usage 全 None。
        - owner 校验：conversation 必须属于 user，否则 404（调用方处理）。
        """
        conv = await self._load_conversation(conversation_public_id, user_internal_id)
        if conv is None:
            raise ConversationNotFound(conversation_public_id)

        if preview_next_request:
            return await self._preview_next_request_usage(
                conv=conv,
                conversation_public_id=conversation_public_id,
                user_internal_id=user_internal_id,
                context_engine=context_engine,
                preview_message=preview_message,
                preview_attached_file_ids=preview_attached_file_ids or [],
            )

        snapshot = await self._load_latest_success_snapshot(
            conversation_id=conv.id, user_internal_id=user_internal_id
        )
        model_cfg = await self._load_model_config(user_internal_id)
        model = self._resolve_model(model_cfg, snapshot=snapshot)

        # The card describes the conversation's canonical working set, not
        # whichever narrow internal LLM profile happened to run last.
        ledger_usage = await self._ledger_usage(
            conv=conv,
            conversation_public_id=conversation_public_id,
            user_internal_id=user_internal_id,
            model=model,
        )
        if ledger_usage is not None:
            return ledger_usage

        if snapshot is None:
            fallback = await self._estimate_without_snapshot(
                conv=conv,
                conversation_public_id=conversation_public_id,
                user_internal_id=user_internal_id,
                model=model,
            )
            if fallback is not None:
                return fallback
            return self._no_snapshot_response(
                conversation_public_id=conversation_public_id,
                model=model,
            )

        # Provider usage is authoritative when a streaming provider returns
        # it.  Only when it is unavailable may the UI present a heuristic.
        exact_input_tokens = getattr(snapshot, "actual_input_tokens", None)
        used_tokens = int(
            exact_input_tokens
            if exact_input_tokens is not None
            else (snapshot.estimated_input_tokens or 0)
        )
        window = model.get("context_window_tokens")
        raw_percent = (used_tokens / window * 100) if window else None

        # 5 分类聚合（unknown kind → 显式抛错，禁止 silent drop）
        raw_breakdown = aggregate_section_stats(snapshot.section_stats_json)
        breakdown = normalize_breakdown_to_used_tokens(raw_breakdown, used_tokens)
        raw_breakdown_total = sum(raw_breakdown.values())
        if raw_breakdown_total > used_tokens:
            logger.warning(
                "CONTEXT_USAGE_BREAKDOWN_NORMALIZED | conversation_id=%s "
                "snapshot_public_id=%s used_tokens=%s raw_breakdown_total=%s",
                conv.id,
                snapshot.public_id,
                used_tokens,
                raw_breakdown_total,
            )

        # Compaction recommended：基于真实 ContextBudget 派生，不硬编码 70/80/90
        compaction = await self._build_compaction_signal(
            snapshot=snapshot,
            used_tokens=used_tokens,
            user_internal_id=user_internal_id,
        )

        allocated_tokens = sum(breakdown.values())
        usage: dict[str, Any] = {
            "used_tokens": used_tokens,
            "available_tokens": max(window - used_tokens, 0) if window else None,
            "percent": round(raw_percent, 2) if raw_percent is not None else None,
            "count_mode": self._resolve_count_mode(snapshot),
            "estimated": self._resolve_count_mode(snapshot) != "exact",
            "over_limit": bool(window and used_tokens > window),
            "unattributed_tokens": max(used_tokens - allocated_tokens, 0),
        }

        recent_snapshots = await self._load_recent_task_success_snapshots(snapshot)
        recent_receipts = [build_evidence_receipt(item) for item in recent_snapshots]
        if not recent_receipts:
            recent_receipts = [build_evidence_receipt(snapshot)]

        return ContextUsageData(
            conversation_public_id=conversation_public_id,
            model=model,
            usage=usage,
            breakdown=breakdown,
            compaction=compaction,
            as_of=_iso(snapshot.completed_at or snapshot.created_at),
            available=True,
            snapshot_public_id=snapshot.public_id,
            debug_details_enabled=self._debug_details_enabled(),
            evidence_receipt=build_evidence_receipt(snapshot),
            recent_evidence_receipts=recent_receipts,
        )

    async def _preview_next_request_usage(
        self,
        *,
        conv,
        conversation_public_id: str,
        user_internal_id: int,
        context_engine,
        preview_message: str | None,
        preview_attached_file_ids: list[str],
    ) -> ContextUsageData:
        """Return the stable ledger while the user is typing.

        Draft text is deliberately not treated as a committed conversation
        turn. Showing a per-draft assembled prompt was the source of visible
        jumps; the next request still receives its normal chat preflight.
        """
        model_cfg = await self._load_model_config(user_internal_id)
        # Keep the same model-window fallback as the idle card.  Otherwise a
        # user merely starting to type could turn a known snapshot window into
        # ``unknown`` and make the stable ledger percentage disappear.
        try:
            snapshot = await self._load_latest_success_snapshot(
                conversation_id=int(conv.id), user_internal_id=user_internal_id
            )
        except Exception as exc:  # noqa: BLE001 - preview has a safe unknown-window fallback
            logger.warning(
                "CONTEXT_USAGE_PREVIEW_SNAPSHOT_UNAVAILABLE | conversation_id=%s | error=%s",
                getattr(conv, "id", None), type(exc).__name__,
            )
            snapshot = None
        model = self._resolve_model(model_cfg, snapshot=snapshot)
        ledger_usage = await self._ledger_usage(
            conv=conv,
            conversation_public_id=conversation_public_id,
            user_internal_id=user_internal_id,
            model=model,
        )
        if ledger_usage is not None:
            return ledger_usage
        if context_engine is None or not callable(getattr(context_engine, "preview", None)):
            return self._preview_unavailable_response(conversation_public_id, model)

        is_baseline = not bool((preview_message or "").strip())
        message = (preview_message or "").strip()
        try:
            persisted_focus, persisted_focus_id, latest_task_id = await self._resolve_preview_focus(
                conv=conv,
                user_internal_id=user_internal_id,
            )
            message = persisted_focus if is_baseline else message
            retrieval_query = _bounded_preview_query(
                persisted_focus if is_baseline else message
            )
            project_context = await self._resolve_preview_project_context(
                conv=conv,
                user_internal_id=user_internal_id,
                query=retrieval_query,
                task_id=latest_task_id,
            )
            request = self._build_preview_request(
                conv=conv,
                conversation_public_id=conversation_public_id,
                user_internal_id=user_internal_id,
                message=message,
                current_message_id=persisted_focus_id if is_baseline else None,
                retrieval_query=retrieval_query,
                task_id=latest_task_id,
                attached_file_ids=preview_attached_file_ids,
                project_context=project_context,
                is_baseline=is_baseline,
            )
            composed = await context_engine.preview(
                request,
                runtime_context=SimpleNamespace(
                    session_factory=_preview_session_factory,
                    user_internal_id=user_internal_id,
                    conversation_internal_id=int(conv.id),
                    conversation_public_id=conversation_public_id,
                    project_context=project_context,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - card must not affect chat
            logger.warning(
                "CONTEXT_USAGE_PREVIEW_UNAVAILABLE | conversation_id=%s | error=%s",
                getattr(conv, "id", None), type(exc).__name__,
            )
            return self._preview_unavailable_response(conversation_public_id, model)

        used_tokens = int(getattr(composed, "estimated_input_tokens", 0) or 0)
        window = model.get("context_window_tokens")
        raw_percent = (used_tokens / window * 100) if window else None
        raw_breakdown = aggregate_section_stats(getattr(composed, "section_stats", None))
        breakdown = normalize_breakdown_to_used_tokens(raw_breakdown, used_tokens)
        preflight = getattr(composed, "preflight", None)
        waterline = str((preflight or {}).get("waterline") or "target")
        return ContextUsageData(
            conversation_public_id=conversation_public_id,
            model=model,
            usage={
                "used_tokens": used_tokens,
                "available_tokens": max(window - used_tokens, 0) if window else None,
                "percent": round(raw_percent, 2) if raw_percent is not None else None,
                "count_mode": "heuristic",
                "estimated": True,
                "over_limit": bool(window and used_tokens > window),
                "unattributed_tokens": max(used_tokens - sum(breakdown.values()), 0),
            },
            breakdown=breakdown,
            compaction={
                "available": await self._compaction_enabled(),
                "recommended": waterline != "target",
                "in_progress": False,
            },
            as_of=_iso(datetime.now(timezone.utc)),
            available=True,
            source="next_request_preview",
            preflight=preflight,
        )

    async def _ledger_usage(
        self,
        *,
        conv,
        conversation_public_id: str,
        user_internal_id: int,
        model: dict[str, Any],
    ) -> ContextUsageData | None:
        """Materialize the persistent, source-reference-only working set."""
        # Preserve the primitive before materialization.  A failed flush can
        # expire ORM instances; consulting ``conv.id`` in the exception path
        # would then turn this optional usage card into a 500 response.
        conversation_id = int(conv.id)
        try:
            from app.services.conversation_context_ledger_service import (
                ConversationContextLedgerService,
            )

            window = model.get("context_window_tokens")
            ledger = await ConversationContextLedgerService(self._session).materialize(
                conversation=conv,
                user_id=user_internal_id,
                context_window_tokens=int(window) if window else None,
            )
        except Exception as exc:  # noqa: BLE001 - card never blocks chat
            logger.warning(
                "CONVERSATION_CONTEXT_LEDGER_UNAVAILABLE | conversation_id=%s | error=%s",
                conversation_id, type(exc).__name__,
            )
            return None

        used_tokens = int(ledger.total_tokens)
        raw_percent = (used_tokens / window * 100) if window else None
        # Keep the UI recommendation aligned with the normal-chat retention
        # policy (BASE_POLICY_LIGHT_DIALOG.conversation_compact_ratio).
        compact_at = int(window * 0.6) if window else None
        return ContextUsageData(
            conversation_public_id=conversation_public_id,
            model=model,
            usage={
                "used_tokens": used_tokens,
                "available_tokens": max(window - used_tokens, 0) if window else None,
                "percent": round(raw_percent, 2) if raw_percent is not None else None,
                "count_mode": "heuristic",
                "estimated": True,
                "over_limit": bool(window and used_tokens > window),
                "unattributed_tokens": 0,
            },
            breakdown=dict(ledger.breakdown),
            compaction={
                "available": await self._compaction_enabled(),
                "recommended": bool(compact_at is not None and used_tokens >= compact_at),
                "in_progress": False,
            },
            as_of=_iso(ledger.materialized_at),
            available=True,
            snapshot_public_id=ledger.public_id,
            debug_details_enabled=self._debug_details_enabled(),
            source="conversation_context_ledger",
        )

    def _preview_unavailable_response(
        self, conversation_public_id: str, model: dict[str, Any]
    ) -> ContextUsageData:
        data = self._no_snapshot_response(
            conversation_public_id=conversation_public_id,
            model=model,
        )
        return ContextUsageData(
            **{**data.__dict__, "source": "next_request_preview_unavailable"}
        )

    async def _resolve_preview_focus(
        self, *, conv, user_internal_id: int
    ) -> tuple[str, int | None, str | None]:
        from app.repositories.agent_task_repository import AgentTaskRepository
        from app.repositories.message_repository import MessageRepository

        conversation_id = int(conv.id)
        latest_message = await MessageRepository(
            self._session
        ).get_latest_user_text_by_conversation(user_internal_id, conversation_id)
        latest_task = await AgentTaskRepository(
            self._session
        ).get_latest_by_conversation(user_internal_id, conversation_id)
        focus = str(getattr(latest_message, "content", None) or "").strip()
        focus_id = getattr(latest_message, "id", None)
        task_id = str(getattr(latest_task, "public_id", None) or "").strip() or None
        return focus, int(focus_id) if focus_id is not None else None, task_id

    async def _resolve_preview_project_context(
        self, *, conv, user_internal_id: int, query: str, task_id: str | None
    ) -> dict[str, Any] | None:
        if getattr(conv, "project_id", None) is None:
            return None
        from app.services.project_context_resolver import ProjectContextResolver

        package = await ProjectContextResolver(self._session).resolve(
            user_id=user_internal_id,
            conversation_id=int(conv.id),
            query=query,
            task_id=task_id,
        )
        return package if package.get("project_id") else None

    @staticmethod
    def _build_preview_request(
        *,
        conv,
        conversation_public_id: str,
        user_internal_id: int,
        message: str,
        current_message_id: int | None,
        retrieval_query: str,
        task_id: str | None,
        attached_file_ids: list[str],
        project_context: dict[str, Any] | None,
        is_baseline: bool,
    ):
        from app.context_engine.models.context import ContextRequest

        workspace_key = (project_context or {}).get("workspace_key")
        return ContextRequest(
            user_id=str(user_internal_id),
            conversation_id=str(conv.id),
            conversation_public_id=conversation_public_id,
            call_site="chat.reply",
            task_id=task_id,
            current_user_message=message,
            current_user_message_id=current_message_id,
            retrieval_query=retrieval_query,
            output_contract="text",
            attached_file_ids=list(attached_file_ids),
            workspace_key=str(workspace_key) if workspace_key else None,
            context_usage_baseline=is_baseline,
        )

    # ── Model 解析（优先级：用户配置 > snapshot > unknown）──────

    def _resolve_model(self, model_cfg, *, snapshot=None) -> dict[str, Any]:
        """model 元数据。

        优先级：
        1. 用户 ModelConfig.context_window_tokens（最高）
        2. 最近一次 snapshot 记录的窗口（仅用于历史快照回放）
        3. UNKNOWN（None）
        """
        user_window = getattr(model_cfg, "context_window_tokens", None) if model_cfg else None
        model_name = getattr(model_cfg, "model_name", None) if model_cfg else None
        snapshot_name = getattr(snapshot, "model_name_snapshot", None) if snapshot else None
        snapshot_window = getattr(snapshot, "context_window_tokens", None) if snapshot else None
        effective_model_name = model_name or snapshot_name
        if user_window:
            return {
                "name": effective_model_name,
                "context_window_tokens": int(user_window),
                "window_source": "model_config",
            }
        if snapshot_window:
            return {
                "name": effective_model_name,
                "context_window_tokens": int(snapshot_window),
                "window_source": "snapshot",
            }
        return {
            "name": effective_model_name,
            "context_window_tokens": None,
            "window_source": "unknown",
        }

    # ── Snapshot 加载 ──────────────────────────────────────────────

    async def _load_conversation(self, public_id: str, user_internal_id: int):
        from app.models.conversation import Conversation

        result = await self._session.execute(
            select(Conversation).where(
                Conversation.public_id == public_id,
                Conversation.user_id == user_internal_id,
                Conversation.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def _load_model_config(self, user_internal_id: int):
        from app.repositories.model_config_repository import (
            ModelConfigRepository,
        )

        repo = ModelConfigRepository(self._session)
        try:
            return await repo.get_active_for_user(user_internal_id)
        except Exception as exc:  # noqa: BLE001 — model config 读取失败不阻塞 usage
            logger.warning("ContextUsageService model config load failed: %s", exc)
            return None

    async def _load_latest_success_snapshot(self, *, conversation_id: int, user_internal_id: int):
        """Load the latest completed snapshot that produced user-visible work.

        Background extraction/compression snapshots are deliberately excluded:
        choosing one would make the card describe the maintenance call instead
        of the reply the user just received.
        """
        from app.models.context_snapshot import ContextSnapshot

        statement = (
            select(ContextSnapshot)
            .where(
                ContextSnapshot.conversation_id == conversation_id,
                ContextSnapshot.user_id == user_internal_id,
                ContextSnapshot.status == "completed",
                ContextSnapshot.context_kind == "active",
            )
            .order_by(ContextSnapshot.id.desc())
            .limit(1)
        )
        # Legacy snapshots can have ``call_site=NULL``; preserve them while
        # filtering only known background sites.
        for pattern in _BACKGROUND_CALL_SITE_PATTERNS:
            statement = statement.where(
                or_(
                    ContextSnapshot.call_site.is_(None),
                    ~ContextSnapshot.call_site.like(pattern),
                )
            )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def _load_recent_task_success_snapshots(self, snapshot) -> list[Any]:
        """Load the small, task-scoped receipt history for user verification.

        A test-plan task can make several CE calls (prepare, generate, review,
        narrative).  Showing only the final call would make a narrative
        snapshot look like proof of the main generator, so receipts are scoped
        to the same task and returned newest-first.
        """
        task_id = getattr(snapshot, "agent_task_id", None)
        if not task_id:
            return [snapshot]

        from app.models.context_snapshot import ContextSnapshot

        result = await self._session.execute(
            select(ContextSnapshot)
            .where(
                ContextSnapshot.agent_task_id == task_id,
                ContextSnapshot.user_id == snapshot.user_id,
                ContextSnapshot.status == "completed",
                ContextSnapshot.context_kind == "active",
            )
            .order_by(ContextSnapshot.id.desc())
            .limit(8)
        )
        return list(result.scalars().all())

    def _resolve_count_mode(self, snapshot) -> str:
        """Provider usage is exact; absence remains an explicit heuristic."""
        return "exact" if getattr(snapshot, "actual_input_tokens", None) is not None else "heuristic"

    @staticmethod
    def _debug_details_enabled() -> bool:
        from app.context_engine.feature_flags import get_context_engine_flags

        return bool(get_context_engine_flags().context_usage_debug_details_enabled)

    # ── Compaction 信号（基于 ContextBudget，不硬编码阈值）──────────

    async def _build_compaction_signal(
        self,
        *,
        snapshot,
        used_tokens: int,
        user_internal_id: int,
    ) -> dict[str, Any]:
        """compaction: {available, recommended, in_progress}。

        recommended = used_tokens >= soft_threshold（从 budget 派生）。
        """
        available = bool(
            await self._compaction_enabled()
        )
        soft_threshold = None
        if snapshot.target_input_tokens:
            soft_threshold = snapshot.target_input_tokens
        elif snapshot.input_budget_tokens:
            soft_threshold = snapshot.input_budget_tokens
        recommended = bool(
            soft_threshold and used_tokens >= soft_threshold
        )
        return {
            "available": available,
            "recommended": recommended,
            "in_progress": False,
        }

    async def _compaction_enabled(self) -> bool:
        try:
            from app.context_engine.feature_flags import get_context_engine_flags

            flags = get_context_engine_flags()
            return bool(
                flags.context_engine_enabled
                and flags.context_compaction_enabled
            )
        except Exception:  # noqa: BLE001
            return False

    # ── No-snapshot 响应 ───────────────────────────────────────────

    async def _estimate_without_snapshot(
        self,
        *,
        conv,
        conversation_public_id: str,
        user_internal_id: int,
        model: dict[str, Any],
    ) -> ContextUsageData | None:
        """Build an explicit heuristic response when old data has no usable snapshot."""
        try:
            ctx = await ConversationContextService(self._session).build_chat_context(
                user_id=user_internal_id,
                conversation_id=int(conv.id),
                current_message_id=None,
                max_turns=10,
            )
            used_tokens = int(getattr(ctx, "estimated_tokens", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "FALLBACK_FAILED | component=context_usage_service | "
                "fallback=conversation_context_estimate | conversation_id=%s | error=%s",
                getattr(conv, "id", None),
                exc,
            )
            return None

        if used_tokens <= 0:
            return None

        logger.warning(
            "FALLBACK_USED | component=context_usage_service | "
            "from=llm_context_snapshots | to=conversation_context_estimate | "
            "reason=no_completed_active_snapshot | conversation_id=%s | "
            "conversation_public_id=%s | used_tokens=%s",
            getattr(conv, "id", None),
            conversation_public_id,
            used_tokens,
        )

        window = model.get("context_window_tokens")
        raw_percent = (used_tokens / window * 100) if window else None
        breakdown = {cat: 0 for cat in UI_CATEGORIES}
        breakdown["conversation_history"] = used_tokens
        breakdown = normalize_breakdown_to_used_tokens(breakdown, used_tokens)

        return ContextUsageData(
            conversation_public_id=conversation_public_id,
            model=model,
            usage={
                "used_tokens": used_tokens,
                "available_tokens": max(window - used_tokens, 0) if window else None,
                "percent": round(raw_percent, 2) if raw_percent is not None else None,
                "count_mode": "heuristic",
                "estimated": True,
                "over_limit": bool(window and used_tokens > window),
            },
            breakdown=breakdown,
            compaction={"available": False, "recommended": False, "in_progress": False},
            as_of=_iso(datetime.now(timezone.utc)),
            available=False,
        )

    def _no_snapshot_response(
        self, *, conversation_public_id: str, model: dict[str, Any]
    ) -> ContextUsageData:
        return ContextUsageData(
            conversation_public_id=conversation_public_id,
            model=model,
            usage={
                "used_tokens": None,
                "available_tokens": None,
                "percent": None,
                "count_mode": "unknown",
                "estimated": None,
                "over_limit": False,
            },
            breakdown={cat: 0 for cat in UI_CATEGORIES},
            compaction={"available": False, "recommended": False, "in_progress": False},
            as_of=None,
            available=False,
        )


class ConversationNotFound(Exception):
    """会话不存在或不属于当前用户（调用方转 404）。"""

    def __init__(self, conversation_public_id: str) -> None:
        self.conversation_public_id = conversation_public_id
        super().__init__(f"conversation {conversation_public_id} not found or not owned")


def _iso(dt) -> str | None:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


__all__ = [
    "ContextUsageService",
    "ContextUsageData",
    "aggregate_section_stats",
    "build_evidence_receipt",
    "normalize_breakdown_to_used_tokens",
    "map_kind_to_ui_category",
    "UI_CATEGORIES",
    "UnknownContextKindError",
    "ConversationNotFound",
]


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (ContextEngine 容量可见性):
#
#   链路:
#     用户在聊天输入或新会话触发时:
#       → api/v1/conversations.py 或 ChatView 取上下文容量
#         → ContextUsageService.get_current_usage(conversation_id, user_id)
#           → 读 llm_context_snapshots 最近一次 status='completed' 且
#             context_kind='active' 的快照
#           → 聚合 selected_documents / selected_messages / selected_memories
#             / system_prompt / tool_definitions / k_tokens / max_tokens
#           → 返回前端 useContextUsage composable
#
# 关键约束(供开发者速查):
#   - WP-BE-02: Context Usage 的定义是"最近一次成功 Context Assembly 实际选择
#     进入模型上下文的规模",**不是**所有历史总和;
#   - 快照来源优先级:llm_context_snapshots > conversation_state 派生;
#   - max_tokens 来源:model_configs.max_tokens (per-user) -> 默认 8000;
#   - 仅作"显示/警告",不影响实际 LLM 调用(后者走 ContextEngine 单独算)。
