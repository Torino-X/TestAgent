"""Runtime tool handlers for Dynamic Agent capabilities."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from app.agent_runtime._shared.public_narrative import AgentPublicUpdateDraft
from app.agent_runtime._shared.narrative_governance.settings_service import (
    is_tool_card_narrative_generation_enabled,
)
from app.agent_runtime.dynamic_agent.schemas import DynamicPlanStep
from app.agent_runtime.feature_flags import get_feature_flags
from app.agent_runtime.narrative_composer.composer import NarrativeComposer
from app.agent_runtime.narrative_composer.context_builders import get_tool_context_builder

logger = logging.getLogger(__name__)


class UploadedFileLike(Protocol):
    public_id: str
    user_id: int
    conversation_id: int
    file_ext: str
    deleted_at: Any


FileResolver = Callable[..., Awaitable[UploadedFileLike | None]]


async def resolve_uploaded_file(*, user_id: int, file_public_id: str, ctx_runtime: Any):
    async with ctx_runtime.session_factory() as session:
        from app.repositories.file_repository import FileRepository

        return await FileRepository(session).get_by_public_id(user_id, file_public_id)


class WordDocumentParseHandler:
    def __init__(
        self,
        *,
        ctx_runtime: Any,
        file_resolver: FileResolver | None = None,
    ) -> None:
        self._ctx = ctx_runtime
        self._file_resolver = file_resolver or resolve_uploaded_file

    async def __call__(self, step: DynamicPlanStep, state: dict) -> dict[str, Any]:
        tool_adapter = getattr(self._ctx, "tool_adapter", None)
        if tool_adapter is None:
            return _failed(
                "CAPABILITY_HANDLER_NOT_CONFIGURED",
                "Runtime tool adapter is not configured.",
            )

        file_public_id = _resolve_file_public_id(step, state)
        if not file_public_id:
            return _failed("INVALID_INPUTS", "word_document_parse requires a file_public_id.")

        user_id = int(getattr(self._ctx, "user_internal_id", 0) or 0)
        if self._file_resolver is resolve_uploaded_file:
            uploaded = await self._file_resolver(
                user_id=user_id,
                file_public_id=file_public_id,
                ctx_runtime=self._ctx,
            )
        else:
            uploaded = await self._file_resolver(
                user_id=user_id,
                file_public_id=file_public_id,
            )
        security_error = _validate_file_access(uploaded, self._ctx)
        if security_error is not None:
            return security_error

        envelope = await tool_adapter.execute(
            tool_name="RequirementParserTool",
            inputs={"requirement_file_id": file_public_id},
            ctx_runtime=self._ctx,
            graph_state=dict(state),
        )
        if not envelope.get("success"):
            await _compose_tool_narrative_if_enabled(
                ctx=self._ctx,
                tool_adapter=tool_adapter,
                tool_name="RequirementParserTool",
                tool_inputs={"requirement_file_id": file_public_id},
                envelope=envelope,
                state=state,
            )
            return {
                "success": False,
                "summary": str(envelope.get("summary") or "RequirementParserTool failed."),
                "data": {},
                "warnings": list(envelope.get("warnings") or []),
                "error": envelope.get("error") or {"code": "REQUIREMENT_PARSE_FAILED"},
            }

        data = _parsed_document_data(envelope.get("data"))
        narrative_state = dict(state)
        narrative_state["requirement_analysis"] = data
        narrative_state.setdefault("requirement_file_id", file_public_id)
        await _compose_tool_narrative_if_enabled(
            ctx=self._ctx,
            tool_adapter=tool_adapter,
            tool_name="RequirementParserTool",
            tool_inputs={"requirement_file_id": file_public_id},
            envelope={**envelope, "data": data},
            state=narrative_state,
        )
        return {
            "success": True,
            "summary": str(envelope.get("summary") or "Document parsed."),
            "data": data,
            "warnings": list(envelope.get("warnings") or []),
            "error": None,
        }


class KnowledgeSearchHandler:
    def __init__(self, *, ctx_runtime: Any) -> None:
        self._ctx = ctx_runtime

    async def __call__(self, step: DynamicPlanStep, state: dict) -> dict[str, Any]:
        step_id = str(getattr(step, "step_id", "") or "")
        if not _knowledge_search_allowed(state):
            return _failed(
                "KNOWLEDGE_SEARCH_NOT_ALLOWED",
                "Retrieval plan does not allow knowledge search.",
            )

        tool_adapter = getattr(self._ctx, "tool_adapter", None)
        if tool_adapter is None:
            return _failed(
                "CAPABILITY_HANDLER_NOT_CONFIGURED",
                "Runtime tool adapter is not configured.",
            )

        envelope = await tool_adapter.execute(
            tool_name="KnowledgeSearchTool",
            inputs=_knowledge_search_inputs(state),
            ctx_runtime=self._ctx,
            graph_state=dict(state),
        )
        await _compose_tool_narrative_if_enabled(
            ctx=self._ctx,
            tool_adapter=tool_adapter,
            tool_name="KnowledgeSearchTool",
            tool_inputs=_knowledge_search_inputs(state),
            envelope=envelope,
            state=state,
        )
        if not envelope.get("success"):
            data = _degraded_knowledge_search_data(
                envelope=envelope,
                query=str(_knowledge_search_inputs(state).get("query") or ""),
            )
            logger.warning(
                "COMPANY_RAG_DYNAMIC_DEGRADED | task_id=%s | step=%s | error_code=%s | query=%s",
                getattr(self._ctx, "task_internal_id", None),
                step_id,
                (envelope.get("error") or {}).get("code") if isinstance(envelope.get("error"), dict) else None,
                data.get("query"),
            )
            return {
                "success": True,
                "summary": "知识库查询失败，已降级为无知识库依据继续执行。",
                "data": data,
                "warnings": list(envelope.get("warnings") or []),
                "error": None,
            }

        data = _knowledge_search_data(envelope.get("data"))
        summary = str(envelope.get("summary") or "Knowledge search completed.")
        if _strict_knowledge_mode(state) and int(data.get("hit_count") or 0) == 0:
            summary = "No knowledge hits found under MAAS_STRICT."
        return {
            "success": True,
            "summary": summary,
            "data": data,
            "warnings": list(envelope.get("warnings") or []),
            "error": None,
        }


def _resolve_file_public_id(step: DynamicPlanStep, state: dict) -> str:
    for ref in step.input_refs:
        ref = str(ref or "")
        if ref.startswith("file:"):
            return ref.split(":", 1)[1]
        if ref.startswith("attachment:"):
            public_id = _attachment_ref_to_public_id(ref, state)
            if public_id:
                return public_id
    return str(state.get("file_public_id") or "").strip()


def _attachment_ref_to_public_id(ref: str, state: dict) -> str:
    try:
        index = int(ref.split(":", 1)[1])
    except (IndexError, ValueError):
        return ""
    attachments = list(state.get("attachment_refs") or [])
    if index < 0 or index >= len(attachments):
        return ""
    item = attachments[index]
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("file_public_id") or item.get("public_id") or "").strip()
    return ""


def _validate_file_access(uploaded: Any, ctx_runtime: Any) -> dict[str, Any] | None:
    if uploaded is None:
        return _failed("FILE_NOT_FOUND_OR_FORBIDDEN", "File is missing or not accessible.")
    if getattr(uploaded, "deleted_at", None) is not None:
        return _failed("FILE_DELETED", "File has been deleted.")
    expected_user = int(getattr(ctx_runtime, "user_internal_id", 0) or 0)
    if int(getattr(uploaded, "user_id", 0) or 0) != expected_user:
        return _failed("FILE_USER_MISMATCH", "File does not belong to the current user.")
    expected_conversation = int(getattr(ctx_runtime, "conversation_internal_id", 0) or 0)
    if int(getattr(uploaded, "conversation_id", 0) or 0) != expected_conversation:
        return _failed(
            "FILE_CONVERSATION_MISMATCH",
            "File does not belong to the current conversation.",
        )
    if _normalized_uploaded_ext(uploaded) != ".docx":
        return _failed("UNSUPPORTED_MEDIA_TYPE", "word_document_parse only supports .docx.")
    return None


def _parsed_document_data(raw: Any) -> dict[str, Any]:
    data = _unwrap_document_data(raw)
    text_content = str(
        data.get("text_content")
        or data.get("content")
        or data.get("plain_text")
        or data.get("markdown")
        or ""
    )
    document_structure = _as_list(
        data.get("document_structure")
        or data.get("structure")
        or data.get("sections")
        or data.get("chapters")
    )
    table_summaries = _as_list(
        data.get("table_summaries")
        or data.get("tables")
        or data.get("table_summary")
    )
    image_texts = _as_list(
        data.get("image_texts")
        or data.get("images")
        or data.get("image_summaries")
    )
    if not text_content and not document_structure and raw:
        logger.warning(
            "DYNAMIC_AGENT_EMPTY_PARSED_DOCUMENT | component=dynamic_agent.word_parse | "
            "reason=normalized_document_data_empty | raw_keys=%s | nested_keys=%s",
            sorted(raw.keys()) if isinstance(raw, dict) else type(raw).__name__,
            sorted(data.keys()) if isinstance(data, dict) else type(data).__name__,
        )
    return {
        "text_content": text_content,
        "document_structure": document_structure,
        "table_summaries": table_summaries,
        "image_texts": image_texts,
        "document_name": str(data.get("document_name") or ""),
    }


def _unwrap_document_data(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    candidates: list[Any] = [raw]
    for key in (
        "data",
        "result",
        "payload",
        "requirement_analysis",
        "document",
        "parsed_document",
        "parse_result",
    ):
        value = raw.get(key)
        if isinstance(value, dict):
            candidates.append(value)
            nested = value.get("data")
            if isinstance(nested, dict):
                candidates.append(nested)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if any(
            key in candidate
            for key in (
                "text_content",
                "document_structure",
                "table_summaries",
                "image_texts",
                "sections",
                "chapters",
                "plain_text",
            )
        ):
            return candidate
    return raw


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, "", {}):
        return []
    return [value]


def _knowledge_search_allowed(state: dict) -> bool:
    retrieval_plan = state.get("retrieval_plan_snapshot")
    retrieval_plan = retrieval_plan if isinstance(retrieval_plan, dict) else {}
    maas = str(retrieval_plan.get("maas") or "").lower()
    if maas == "off":
        return False
    if maas in {"required", "auto"}:
        return True
    return str(state.get("knowledge_mode_snapshot") or "").upper() in {
        "MAAS_STRICT",
        "KNOWLEDGE_REQUIRED",
    }


def _strict_knowledge_mode(state: dict) -> bool:
    return str(state.get("knowledge_mode_snapshot") or "").upper() == "MAAS_STRICT"


def _knowledge_search_inputs(state: dict) -> dict[str, Any]:
    retrieval_plan = state.get("retrieval_plan_snapshot")
    retrieval_plan = retrieval_plan if isinstance(retrieval_plan, dict) else {}
    query = str(
        retrieval_plan.get("query")
        or state.get("goal")
        or state.get("dynamic_goal")
        or ""
    ).strip()
    inputs: dict[str, Any] = {"query": query}
    top_k = retrieval_plan.get("top_k")
    if top_k is not None:
        inputs["top_k"] = int(top_k)
    return inputs


def _knowledge_search_data(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    hits = data.get("hits")
    if hits is None:
        hits = []
        for key in ("similar_projects", "standards", "terms"):
            values = data.get(key)
            if isinstance(values, list):
                hits.extend(values)
    hit_count = data.get("hit_count")
    if hit_count is None:
        hit_count = len(hits) if isinstance(hits, list) else 0
    normalized = dict(data)
    normalized["hits"] = list(hits) if isinstance(hits, list) else []
    normalized["hit_count"] = int(hit_count or 0)
    return normalized


def _degraded_knowledge_search_data(*, envelope: dict[str, Any], query: str) -> dict[str, Any]:
    error = envelope.get("error") or {}
    if not isinstance(error, dict):
        error = {"message": str(error)}
    return {
        "similar_projects": [],
        "standards": [],
        "terms": [],
        "hits": [],
        "query": query,
        "hit_count": 0,
        "confidence": "low",
        "elapsed_ms": 0,
        "direct_answer_eligible": False,
        "degraded": True,
        "source": "company_rag",
        "skip_reason": "tool_failed",
        "error_code": str(error.get("code") or "KNOWLEDGE_SEARCH_FAILED"),
        "error_message": str(error.get("message") or envelope.get("summary") or ""),
    }


async def _compose_tool_narrative_if_enabled(
    *,
    ctx: Any,
    tool_adapter: Any,
    tool_name: str,
    tool_inputs: dict[str, Any],
    envelope: dict[str, Any],
    state: dict,
) -> None:
    if (
        not get_feature_flags().phase29b_tool_narrative_enabled
        or not await is_tool_card_narrative_generation_enabled(ctx)
    ):
        return
    sink = getattr(ctx, "event_sink", None)
    bridge = getattr(ctx, "context_llm_invoker", None)
    if bridge is None or not getattr(bridge, "available", False) or sink is None:
        logger.warning(
            "FALLBACK_USED | component=dynamic_agent.tool_narrative | "
            "from=llm_tool_narrative | to=deterministic_tool_update | "
            "reason=runtime_llm_or_event_sink_missing | task_id=%s | tool=%s",
            str(getattr(ctx, "task_internal_id", "")),
            tool_name,
        )
        return
    llm_client = bridge.bind(
        user_id=int(getattr(ctx, "user_internal_id", 0) or 0),
        call_site="dynamic_agent.tool_narrative",
        task_id=getattr(ctx, "task_internal_id", None),
        runtime_context=ctx,
    )
    tool_call_id = str(envelope.get("tool_call_id") or "").strip()
    if not tool_call_id:
        logger.warning(
            "FALLBACK_USED | component=dynamic_agent.tool_narrative | "
            "from=llm_tool_narrative | to=deterministic_tool_update | "
            "reason=tool_call_id_missing | task_id=%s | tool=%s",
            str(getattr(ctx, "task_internal_id", "")),
            tool_name,
        )
        return

    source_event_id = ""
    last_terminal = getattr(tool_adapter, "last_terminal_event_id", None)
    if callable(last_terminal):
        source_event_id = str(last_terminal() or "")

    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
    narrative_state = dict(state or {})
    if tool_name == "KnowledgeSearchTool":
        kb_result = dict(data or {})
        kb_result.setdefault("query", str(tool_inputs.get("query") or ""))
        if not envelope.get("success", False):
            kb_result.setdefault(
                "skip_reason",
                str(error.get("code") or error.get("message") or "tool_failed"),
            )
            kb_result.setdefault("hit_count", 0)
            kb_result.setdefault("used_count", 0)
        narrative_state["knowledge_search_result"] = kb_result

    terminal_status = "success" if envelope.get("success", False) else "failed"
    if data.get("disabled_by_config") or data.get("skip_reason"):
        terminal_status = "skipped"

    context = get_tool_context_builder(tool_name).build(
        graph_state=narrative_state,
        tool_call_id=tool_call_id,
        source_event_id=source_event_id,
        attempt=int(envelope.get("attempt") or 1),
        terminal_status=terminal_status,
        duration_ms=envelope.get("duration_ms"),
        continuation_route="dynamic_agent_continue",
    )
    composer = NarrativeComposer(
        llm_client,
        sink,
        deterministic_fallback=AgentPublicUpdateDraft(
            headline="\u5df2\u8bb0\u5f55\u5de5\u5177\u6267\u884c\u7ed3\u679c",
            summary=str(envelope.get("summary") or "\u5de5\u5177\u8c03\u7528\u5df2\u8fd4\u56de\u3002")[:200],
            impact="\u7cfb\u7edf\u5c06\u6839\u636e\u5f53\u524d\u7ed3\u679c\u7ee7\u7eed\u63a8\u8fdb\u4efb\u52a1\u3002",
            next_action="\u7ee7\u7eed\u6267\u884c\u52a8\u6001\u4efb\u52a1\u540e\u7eed\u6b65\u9aa4\u3002",
        ),
        timeout_seconds=get_feature_flags().phase29b_narrative_timeout_seconds,
        repair_attempts=get_feature_flags().phase29b_narrative_repair_attempts,
    )
    try:
        await composer.compose_tool_narrative(
            task_internal_id=ctx.task_internal_id,
            graph_run_id=f"run-{ctx.task_internal_id}",
            context=context,
            narrative_id=f"nar_{uuid.uuid4().hex[:12]}",
            generation_id=f"gen_{uuid.uuid4().hex[:12]}",
            generation_no=1,
            event_prefix="tool",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "FALLBACK_USED | component=dynamic_agent.tool_narrative | "
            "from=llm_tool_narrative | to=deterministic_tool_update | "
            "reason=narrative_exception:%s | task_id=%s | tool=%s",
            type(exc).__name__,
            str(getattr(ctx, "task_internal_id", "")),
            tool_name,
        )


def _failed(code: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "summary": message,
        "data": {},
        "warnings": [],
        "error": {"code": code, "message": message, "recoverable": False},
    }


def _normalized_uploaded_ext(uploaded: Any) -> str:
    ext = str(getattr(uploaded, "file_ext", "") or "").strip().lower()
    if ext:
        return ext if ext.startswith(".") else f".{ext}"
    name = str(getattr(uploaded, "original_name", "") or "").strip()
    suffix = Path(name).suffix.lower()
    if suffix:
        return suffix
    mime = str(getattr(uploaded, "mime_type", "") or "").strip().lower()
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return ".docx"
    return ""


__all__ = [
    "FileResolver",
    "KnowledgeSearchHandler",
    "WordDocumentParseHandler",
    "resolve_uploaded_file",
]


# module-level note (auto-appended):
# Tool handlers for dynamic agent. 注册 tool 路由(call_site → 执行函数)。
# 关键约束: 与 permission 同步白名单。
