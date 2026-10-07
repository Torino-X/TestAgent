"""Chat LLM service — handles ordinary Q&A replies (F013 + F014 + F016).

Unlike the test-plan generation tools, ``ChatLLMService`` does not
write tool_calls, does not create ``AgentTask`` records, and does not
emit agent events.  It returns natural-language text that the
``MessageService`` will persist as a normal assistant ``Message``.

F014: switched to ``LLMClient.generate_with_profile(CHAT_PROFILE)`` so
that ordinary chat replies allow Markdown (headings / lists / code
fences / tables).  The previous implementation called the private
``ResultParser._sanitize_text`` and truncated to 400 characters —
both of those behaviours are gone.  Length is now controlled by the
profile's ``output_schema.max_chars`` knob (configured via the
service's ``max_chars`` argument; default 4000).

F016: ``generate_reply`` and ``stream_reply`` accept an optional
``ChatContext`` built by ``ConversationContextService``.  When
present, the prompt is structured with sections for summary, recent
turns, file summaries, and task state.  When absent, the legacy
single-turn path is used (backward compatible).

If the LLM call or parse fails, a polite fallback string is returned
so the user is never left without a response.  ``CHAT_FALLBACK_TEXT``
re-exports ``CHAT_FALLBACK_REPLY`` from
``app.llm.task_profiles`` — that module is the single source of
truth shared with ``message_service`` (the two services cannot
import each other to avoid a top-level cycle).

════════════════════════════════════════════════════════════════════════════════
链路位置 (普通聊天的 LLM 调用层):

  普通聊天 API:
    api/v1/messages.py → MessageService.send_message(...)
      → ChatLLMService.generate_reply(prompt, chat_context=ctx, user_internal_id)
        → LLMClient.generate_with_profile(CHAT_PROFILE, prompt)
        → markdown 文本返回
      → MessageService 落库 normal assistant Message(普通消息,不写 tool_calls)

  流式:
    api/v1/messages.py → ChatLLMService.stream_reply(...)
      → 增量 yield(text),前端用 SSE 渲染

关键约束(供开发者速查):
  - 本服务 **不** 写 AgentTask / AgentEvent / agent_events SSE;
  - 不触发 TestPlanGeneratorTool 等任何 8 个工具调用;
  - chat_context=None 走 legacy 单轮路径,F016 多轮场景才用 ConversationContextService
    拼接 summary/recent turns/files/task state;
  - max_chars 由 CHAT_PROFILE.output_schema.max_chars 控制(配置入口)而非硬编码。
════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator, Optional

from app.llm.task_profiles import CHAT_FALLBACK_REPLY, CHAT_PROFILE
from app.integrations.llm_client import LLMClient, LLMProfileResult

logger = logging.getLogger(__name__)


# Backwards-compatible alias — the rest of the codebase (including
# existing tests) imports ``CHAT_FALLBACK_TEXT``.  The canonical value
# lives in ``app.llm.task_profiles.CHAT_FALLBACK_REPLY`` and is shared
# with ``message_service.CHAT_FALLBACK_REPLY``.
CHAT_FALLBACK_TEXT = CHAT_FALLBACK_REPLY

# Internal alias — used as the safe-by-construction fallback when
# the contract layer surfaces no parsed value.
_INTERNAL_FALLBACK = CHAT_FALLBACK_REPLY


_KB_SNIPPET_MAX_CHARS = 400

_DOCUMENT_GROUNDING_REPLIES = {
    "context.source.document_ambiguous": ("ambiguous", "当前会话中有多个可能被指代的资料，请说明文件名、资料类型（需求/模板/测试方案）或版本后再问。"),
    "context.source.document_full_content_too_large": ("full_content_too_large", "该资料完整正文超出当前模型一次可安全读取的上下文窗口；系统不会用少量检索片段替代全文。请缩小到章节范围，或使用全文分段阅读流程。"),
    "context.source.document_indexing": ("indexing", "本会话资料正在解析或建立索引，暂不能可靠回答；请稍后再试。"),
    "context.source.document_no_material": ("no_material", "未在本会话中找到可用于问答的已上传资料或生成产物。"),
    "context.source.document_no_evidence": ("no_evidence", "未在本会话资料中找到能够支持该问题的依据。"),
    "context.source.document_retrieval_unavailable": ("retrieval_degraded", "本会话资料检索暂不可用，不能可靠地基于文档回答。"),
    "context.source.document_runtime_unavailable": ("retrieval_degraded", "本会话资料检索暂不可用，不能可靠地基于文档回答。"),
    "context.source.document_retrieval_error": ("retrieval_degraded", "本会话资料检索暂时失败，不能可靠地基于文档回答。"),
}


def _document_grounding_reply(exc: BaseException) -> tuple[str, str, str] | None:
    """Return a deterministic reply only for typed document-QA source failures."""
    error = getattr(exc, "error", None)
    code = str(getattr(error, "code", "") or "")
    outcome = _DOCUMENT_GROUNDING_REPLIES.get(code)
    if outcome is None:
        return None
    status, reply = outcome
    return status, code, reply


def _format_kb_snippets(snippets: list[dict]) -> str:
    """Format knowledge-base snippets as a single【知识库参考】section.

    Each snippet is rendered as a bullet ``- [doc_name] (score) snippet``.
    ``content`` is truncated to ``_KB_SNIPPET_MAX_CHARS`` per snippet so
    that a verbose KB passage doesn't blow the prompt budget.
    """
    lines = ["【知识库参考】"]
    for idx, snippet in enumerate(snippets, 1):
        if not isinstance(snippet, dict):
            continue
        doc_name = (
            snippet.get("doc_name")
            or snippet.get("document_name")
            or snippet.get("title")
            or f"知识库片段{idx}"
        )
        score = snippet.get("score")
        score_label = f"；相似度：{float(score):.2f}" if score is not None else ""
        content = (
            snippet.get("content")
            or snippet.get("text")
            or snippet.get("snippet")
            or ""
        )
        content = str(content).strip()
        if len(content) > _KB_SNIPPET_MAX_CHARS:
            content = content[: _KB_SNIPPET_MAX_CHARS].rstrip() + "…"
        lines.append(f"- 来源：{doc_name}{score_label}\n  {content}")
    return "\n".join(lines)


def _context_conversation_id(chat_context: object | None) -> int | None:
    if chat_context is None:
        return None
    raw = getattr(chat_context, "conversation_id", None)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _context_current_message_id(chat_context: object | None) -> int | None:
    if chat_context is None:
        return None
    raw = getattr(chat_context, "current_message_id", None)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _record_context_bridge_diagnostic(
    diagnostics: dict[str, Any] | None,
    *,
    outcome: str,
    snapshot_public_id: str | None = None,
    exception: BaseException | None = None,
) -> None:
    """Attach a content-free Context Engine outcome to one chat response."""
    if diagnostics is None:
        return
    evidence: dict[str, Any] = {"path": "context_bridge", "outcome": outcome}
    if snapshot_public_id:
        evidence["snapshot_public_id"] = str(snapshot_public_id)
    if exception is not None:
        evidence["exception_type"] = type(exception).__name__
        safe_error = getattr(exception, "error", None)
        error_code = getattr(safe_error, "code", None)
        error_stage = getattr(safe_error, "stage", None)
        if error_code:
            evidence["error_code"] = str(error_code)
        if error_stage:
            evidence["stage"] = str(getattr(error_stage, "value", error_stage))
        if safe_error is not None:
            evidence["retryable"] = bool(getattr(safe_error, "retryable", False))
            metadata = getattr(safe_error, "safe_metadata", {})
            if isinstance(metadata, dict):
                for key in (
                    "reason",
                    "compaction_attempted",
                    "compaction_compactor_available",
                    "compaction_runtime_context_available",
                    "compaction_phase",
                    "compaction_exception_type",
                    "compaction_exception_code",
                ):
                    if key in metadata:
                        evidence[key] = metadata[key]
    diagnostics["chat_context_engine"] = evidence


class ChatLLMService:
    """Generate natural-language chat replies for the user.

    Output is Markdown-cleaned by ``MarkdownParser`` (no control
    labels stripped, no JSON attempted).  Length cap is configurable
    via ``CHAT_PROFILE.output_schema['max_chars']``.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        max_chars: int = 4000,
        context_llm_invoker=None,
        context_engine_invoker=None,
        task_flag_resolver=None,
        session_factory=None,
    ) -> None:
        self._llm = llm_client
        # CE-04：MIG_CHAT=true 时走 ContextInvokerBridge（生产注入
        # ``app.state.context_llm_bridge``）；false 时保持 legacy。
        self._context_llm_invoker = context_llm_invoker
        # The bridge owns the outward chat call.  Preflight compaction needs
        # the underlying ContextAwareLLMInvoker inside runtime_context so it
        # can perform its separate compression-provider call.
        self._context_engine_invoker = context_engine_invoker
        # CE-05 WP-2: 任务级 Flag Resolver（冻结 Manifest）；None → 进程级。
        self._task_flag_resolver = task_flag_resolver
        # WP-BE-09: 普通 Chat 路径走 bridge 时需要 runtime_context
        # （snapshot 落库 session_factory + llm_client）。
        self._session_factory = session_factory
        # Apply the override into the profile's output_schema.  We
        # create a fresh dict per instance so concurrent services
        # cannot stomp each other.
        self._profile = CHAT_PROFILE.model_copy(
            update={
                "output_schema": {
                    "max_chars": int(max_chars) if max_chars and max_chars > 0 else 4000
                }
            }
        )

    async def generate_reply(
        self,
        content: str,
        history: Optional[list[dict]] = None,
        chat_context: object | None = None,
        user_id: Optional[int] = None,
        conversation_public_id: str | None = None,
        diagnostics: dict[str, Any] | None = None,
        call_site: str = "chat.reply",
    ) -> str:
        """Reply to ``content`` (with optional short conversation history).

        Returns the parsed Markdown text on success.  Returns the
        profile's ``fallback_text`` (or a stable internal fallback)
        when the contract layer signals failure.  Never raises.

        CE-04：``user_id`` 供 MIG_CHAT 走 ContextInvokerBridge 时透传；
        legacy 路径不受影响。
        """
        legacy_user_content = (
            self._build_context_content(content or "", chat_context)
            if chat_context is not None
            else self._build_user_content(content or "", history)
        )
        current_message = content or ""
        start = time.monotonic()
        using_context_bridge = True
        logger.info(
            "ChatLLMService.generate_reply: 调用LLM | 内容长度=%d | history_turns=%d",
            len(content or ""), len(history or []),
        )
        try:
            # CE-only: a disabled or unavailable bridge is a deterministic
            # fallback, never permission to rebuild a Legacy prompt.
            mig_chat = True
            if mig_chat:
                bridge = self._context_llm_invoker
                if bridge is None or not getattr(bridge, "available", False):
                    _record_context_bridge_diagnostic(
                        diagnostics, outcome="bridge_unavailable"
                    )
                    logger.warning(
                        "ChatLLMService.generate_reply: MIG_CHAT=true 但 Invoker 不可用",
                    )
                    return self._resolve_fallback(None, None)
                bres = await bridge.generate(
                    user_id=user_id or 0,
                    call_site=call_site,
                    llm_task_profile=self._profile,
                    current_goal=current_message,
                    current_user_message_id=_context_current_message_id(chat_context),
                    output_contract="text",
                    user_content=(
                        legacy_user_content
                        if getattr(chat_context, "project_context", None)
                        else current_message
                    ),
                    runtime_context=self._build_runtime_context(
                        user_id,
                        conversation_id=_context_conversation_id(chat_context),
                        conversation_public_id=conversation_public_id,
                        project_context=getattr(chat_context, "project_context", None),
                    ),
                )
                if bres is None or bres.value is None:
                    _record_context_bridge_diagnostic(
                        diagnostics, outcome="bridge_empty_result"
                    )
                    logger.warning(
                        "ChatLLMService.generate_reply: MIG_CHAT bridge 返回空",
                    )
                    return self._resolve_fallback(None, None)
                _record_context_bridge_diagnostic(
                    diagnostics,
                    outcome="completed",
                    snapshot_public_id=getattr(bres, "snapshot_public_id", None),
                )
                return str(bres.value).strip() or self._resolve_fallback(None, None)
            return self._resolve_fallback(None, None)
        except Exception as exc:  # noqa: BLE001 — belt-and-braces
            document_grounding = (
                _document_grounding_reply(exc)
                if call_site == "document.qa"
                else None
            )
            if document_grounding is not None:
                status, code, reply = document_grounding
                if diagnostics is not None:
                    diagnostics["document_grounding"] = {
                        "status": status,
                        "error_code": code,
                    }
                _record_context_bridge_diagnostic(
                    diagnostics, outcome="document_grounding_blocked", exception=exc
                )
                logger.info(
                    "DOCUMENT_GROUNDING_BLOCKED | status=%s | error_code=%s",
                    status,
                    code,
                )
                return reply
            if using_context_bridge:
                _record_context_bridge_diagnostic(
                    diagnostics, outcome="bridge_exception", exception=exc
                )
            logger.exception("ChatLLMService: contract call failed: %s", exc)
            return self._resolve_fallback(None, exc)

        latency_ms = int((time.monotonic() - start) * 1000)
        if result.success and isinstance(result.parsed, str):
            logger.info(
                "ChatLLMService.generate_reply: 成功 | 耗时=%dms | 字符=%d",
                latency_ms, len(result.parsed),
            )
            return result.parsed

        # Either the LLM call failed or the parser returned a non-string
        # value (should not happen for MarkdownParser, but defend anyway).
        if result.parsed is None:
            logger.warning(
                "ChatLLMService.generate_reply: 解析失败 | 耗时=%dms | error_type=%s",
                latency_ms, result.error_type,
            )
            return self._resolve_fallback(result, None)
        if isinstance(result.parsed, str):
            logger.info(
                "ChatLLMService.generate_reply: 走fallback文本 | 耗时=%dms",
                latency_ms,
            )
            return result.parsed
        return _INTERNAL_FALLBACK

    async def stream_reply(
        self,
        content: str,
        history: Optional[list[dict]] = None,
        chat_context: object | None = None,
        user_id: Optional[int] = None,
        conversation_public_id: str | None = None,
        call_site: str = "chat.reply",
    ) -> AsyncIterator[str]:
        """Stream a Markdown chat reply chunk by chunk."""
        legacy_user_content = (
            self._build_context_content(content or "", chat_context)
            if chat_context is not None
            else self._build_user_content(content or "", history)
        )
        current_message = content or ""
        try:
            from app.context_engine.feature_flags import get_context_engine_flags

            mig_chat = True
            if mig_chat:
                bridge = self._context_llm_invoker
                if bridge is None or not getattr(bridge, "available", False):
                    logger.warning(
                        "FALLBACK_USED | component=chat_llm_service.stream_reply | "
                        "from=context_bridge | to=static_chat_fallback | "
                        "reason=bridge_unavailable | user_id=%s",
                        user_id,
                    )
                    yield self._resolve_fallback(None, None)
                    return
                bridge_kwargs = {
                    "user_id": user_id or 0,
                    "call_site": call_site,
                    "llm_task_profile": self._profile,
                    "current_goal": current_message,
                    "current_user_message_id": _context_current_message_id(chat_context),
                    "output_contract": "text",
                    "user_content": (
                        legacy_user_content
                        if getattr(chat_context, "project_context", None)
                        else current_message
                    ),
                    "runtime_context": self._build_runtime_context(
                        user_id,
                        conversation_id=_context_conversation_id(chat_context),
                        conversation_public_id=conversation_public_id,
                        project_context=getattr(chat_context, "project_context", None),
                    ),
                }
                bridge_stream = getattr(bridge, "stream", None)
                if callable(bridge_stream):
                    emitted = False
                    async for chunk in bridge_stream(**bridge_kwargs):
                        if not chunk:
                            continue
                        emitted = True
                        yield str(chunk)
                    if not emitted:
                        yield self._resolve_fallback(None, None)
                    return
                # Compatibility for an older bridge that only exposes generate().
                bres = await bridge.generate(**bridge_kwargs)
                text = str(getattr(bres, "value", "") or "").strip()
                yield text or self._resolve_fallback(None, None)
                return
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "FALLBACK_USED | component=chat_llm_service.stream_reply | "
                "from=context_bridge | to=static_chat_fallback | "
                "reason=bridge_exception | user_id=%s | err_type=%s | err=%s",
                user_id,
                type(exc).__name__,
                str(exc)[:300],
            )
            yield self._resolve_fallback(None, exc)
            return
        stream_with_profile = getattr(self._llm, "stream_with_profile", None)
        if stream_with_profile is None:
            yield await self.generate_reply(
                content, history, chat_context=chat_context, user_id=user_id
            )
            return
        try:
            emitted = False
            # Chat SSE Lifecycle Fix — Phase 1 runtime trace (no control flow change).
            logger.info(
                "CHAT_PROVIDER_STREAM_ENTER | profile=%s | user_content_len=%d",
                getattr(self._profile, "name", "?"),
                len(legacy_user_content or ""),
            )
            _provider_chunk_count = 0
            _provider_text_len = 0
            for chunk in ():
                if not chunk:
                    continue
                emitted = True
                _provider_chunk_count += 1
                _provider_text_len += len(str(chunk))
                yield str(chunk)
            if not emitted:
                yield _INTERNAL_FALLBACK
            logger.info(
                "CHAT_PROVIDER_STREAM_EXIT | profile=%s | chunks=%d | text_len=%d",
                getattr(self._profile, "name", "?"),
                _provider_chunk_count,
                _provider_text_len,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("ChatLLMService: streaming contract call failed: %s", exc)
            try:
                yield await self.generate_reply(
                    content, history, chat_context=chat_context, user_id=user_id
                )
            except Exception:
                yield self._resolve_fallback(None, exc)

    # ── Internals ─────────────────────────────────────────────────

    def _build_runtime_context(
        self, user_id: int | None, *, conversation_id: int | None = None,
        conversation_public_id: str | None = None,
        project_context: dict | None = None,
    ):
        """构造普通 Chat 路径的轻量 runtime_context（bridge 需要）。

        AgentTask 路径由 ProductionRuntimeContextFactory 提供完整 runtime_context；
        普通 Chat 无此 factory，这里提供 session_factory + llm_client + user_internal_id
        满足 ContextAwareLLMInvoker 的 snapshot 落库与 provider 调用。
        """
        if self._session_factory is None:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(
            session_factory=self._session_factory,
            llm_client=self._llm,
            context_llm_invoker=self._context_engine_invoker,
            user_internal_id=int(user_id or 0),
            conversation_internal_id=conversation_id,
            conversation_public_id=conversation_public_id,
            project_context=(dict(project_context) if isinstance(project_context, dict) else None),
        )

    def _resolve_fallback(
        self,
        result: Optional[LLMProfileResult],
        exc: Optional[BaseException],
    ) -> str:
        """Pick the right fallback string based on profile + exc."""
        if exc is not None:
            logger.warning("ChatLLMService: LLM call failed: %s", exc)
        fallback = self._profile.fallback_text or _INTERNAL_FALLBACK
        return fallback

    @staticmethod
    def _build_user_content(content: str, history: Optional[list[dict]]) -> str:
        if not history:
            return content
        # Keep at most the last 6 turns to bound prompt size.
        recent = history[-6:]
        blocks: list[str] = []
        for turn in recent:
            role = str(turn.get("role", "user")).strip().lower()
            text = str(turn.get("content", "")).strip()
            if not text:
                continue
            label = "用户" if role == "user" else "助手"
            blocks.append(f"{label}：{text}")
        if not blocks:
            return content
        blocks.append(f"用户：{content}")
        return "\n".join(blocks)

    @staticmethod
    def _build_context_content(content: str, ctx: object) -> str:
        """Build structured prompt from F016 ChatContext.

        Sections: summary → recent turns → files → task state → current question.
        """
        parts: list[str] = []

        # Project instructions/memory/sources precede conversation context.
        project_context = getattr(ctx, "project_context", None)
        if isinstance(project_context, dict) and project_context.get("project_id"):
            project_name = str(project_context.get("project_name") or project_context["project_id"])
            parts.append(f"【项目上下文：{project_name}】")
            instructions = project_context.get("instructions") or []
            if instructions:
                parts.append("【项目指令】")
                parts.extend(
                    f"- {str(item.get('content') or '').strip()}"
                    for item in instructions
                    if isinstance(item, dict) and str(item.get("content") or "").strip()
                )
            memories = project_context.get("memories") or []
            if memories:
                parts.append("【已确认的项目记忆】")
                parts.extend(
                    f"- {str(item.get('content') or '').strip()}"
                    for item in memories
                    if isinstance(item, dict) and str(item.get("content") or "").strip()
                )
            source_hits = project_context.get("source_hits") or []
            if source_hits:
                parts.append("【项目资料】")
                for item in source_hits:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or item.get("source_id") or "项目资料")
                    excerpt = str(item.get("content") or "").strip()
                    if excerpt:
                        parts.append(f"- {title}：{excerpt}")
            progress = project_context.get("recent_progress") or []
            if progress:
                parts.append("【项目近期进度】")
                for item in progress:
                    if not isinstance(item, dict):
                        continue
                    summary = str(item.get("summary") or item.get("title") or "").strip()
                    if summary:
                        parts.append(f"- {summary}")

        # Conversation summary
        conv_summary = getattr(ctx, "conversation_summary", None)
        if conv_summary:
            parts.append(f"【会话摘要】\n{conv_summary}")

        # Recent conversation turns
        recent_messages = getattr(ctx, "recent_messages", []) or []
        if recent_messages:
            parts.append("【最近对话】")
            for msg in recent_messages:
                role = getattr(msg, "role", "user")
                text = getattr(msg, "content", "") or ""
                if not text.strip():
                    continue
                label = "用户" if role == "user" else "助手"
                parts.append(f"{label}：{text}")

        # File summaries
        file_summaries = getattr(ctx, "file_summaries", []) or []
        if file_summaries:
            parts.append("【当前会话文件】")
            for f in file_summaries:
                fname = getattr(f, "file_name", "")
                ftype = getattr(f, "file_type", None) or "未知"
                fstatus = getattr(f, "upload_status", None) or "未知"
                parts.append(f"- {fname}；类型：{ftype}；状态：{fstatus}")

        # Latest task summary
        task_summary = getattr(ctx, "latest_task_summary", None)
        if task_summary:
            ttype = getattr(task_summary, "task_type", None) or "测试方案生成"
            tstatus = getattr(task_summary, "status", "unknown")
            task_info = f"任务：{ttype}；状态：{tstatus}"
            pending = getattr(task_summary, "pending_confirmation_count", 0)
            if pending and pending > 0:
                task_info += f"；待确认：{pending} 个"
            parts.append(f"【最近任务状态】\n{task_info}")

        # F026: Knowledge-base snippets (medium/low confidence polish path).
        kb_snippets = getattr(ctx, "knowledge_snippets", None) or []
        if kb_snippets:
            parts.append(_format_kb_snippets(kb_snippets))

        # Current user question
        parts.append(f"【当前用户问题】\n{content}")

        return "\n\n".join(parts)
