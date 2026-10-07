"""Context reducer — controls context size via turn limits and token budgets.

Pure functions with no DB dependencies.  Operates on ORM Message objects
and schema objects, producing bounded ChatContext / IntentContext outputs.
"""

from __future__ import annotations

import logging
from typing import Any

from app.common.token_estimator import estimate_tokens
from app.schemas.context import (
    ChatContext,
    ChatHistoryMessage,
    FileContextSummary,
    IntentContext,
    TaskContextSummary,
)

logger = logging.getLogger(__name__)

# Default budget constants
_DEFAULT_MAX_CHAT_TURNS = 10
_DEFAULT_MAX_INTENT_TURNS = 3
_DEFAULT_MAX_CHAT_TOKENS = 6000
_DEFAULT_MAX_INTENT_TOKENS = 2500
_DEFAULT_MAX_SUMMARY_CHARS = 1200
_DEFAULT_MAX_INTENT_SUMMARY_CHARS = 500
_DEFAULT_MAX_FILE_SUMMARIES = 10
_DEFAULT_MAX_SINGLE_MESSAGE_CHARS = 1500

_TRUNCATION_SUFFIX = "\n[内容过长，已截断]"


class ContextReducer:
    """Controls context size via turn limits and token budgets."""

    def __init__(
        self,
        max_chat_turns: int = _DEFAULT_MAX_CHAT_TURNS,
        max_intent_turns: int = _DEFAULT_MAX_INTENT_TURNS,
        max_chat_tokens: int = _DEFAULT_MAX_CHAT_TOKENS,
        max_intent_tokens: int = _DEFAULT_MAX_INTENT_TOKENS,
        max_summary_chars: int = _DEFAULT_MAX_SUMMARY_CHARS,
        max_intent_summary_chars: int = _DEFAULT_MAX_INTENT_SUMMARY_CHARS,
        max_file_summaries: int = _DEFAULT_MAX_FILE_SUMMARIES,
        max_single_message_chars: int = _DEFAULT_MAX_SINGLE_MESSAGE_CHARS,
    ) -> None:
        self.max_chat_turns = max_chat_turns
        self.max_intent_turns = max_intent_turns
        self.max_chat_tokens = max_chat_tokens
        self.max_intent_tokens = max_intent_tokens
        self.max_summary_chars = max_summary_chars
        self.max_intent_summary_chars = max_intent_summary_chars
        self.max_file_summaries = max_file_summaries
        self.max_single_message_chars = max_single_message_chars

    # ── Public API ───────────────────────────────────────────────

    def reduce_chat_context(
        self,
        messages: list[Any],
        summary: str | None,
        file_summaries: list[FileContextSummary],
        task_summary: TaskContextSummary | None,
        *,
        conversation_id: str = "",
        current_message_id: str | None = None,
        exclude_id: int | None = None,
    ) -> ChatContext:
        """Build a bounded ChatContext from raw materials."""
        recent = self._pick_recent_messages(
            messages, self.max_chat_turns, exclude_id=exclude_id
        )
        truncated_summary = self._truncate_text(summary, self.max_summary_chars)
        limited_files = file_summaries[: self.max_file_summaries]

        history = [
            ChatHistoryMessage(
                role="assistant" if m.role == "agent" else m.role,
                content=self._truncate_text(
                    m.content, self.max_single_message_chars
                ),
                message_id=getattr(m, "public_id", None),
                created_at=getattr(m, "created_at", None),
            )
            for m in recent
        ]

        total_tokens = self._estimate_context_tokens(
            truncated_summary, history, limited_files, task_summary
        )

        return ChatContext(
            conversation_id=conversation_id,
            current_message_id=current_message_id,
            conversation_summary=truncated_summary,
            recent_messages=history,
            file_summaries=limited_files,
            latest_task_summary=task_summary,
            estimated_tokens=total_tokens,
        )

    def reduce_intent_context(
        self,
        messages: list[Any],
        summary: str | None,
        file_summaries: list[FileContextSummary],
        task_summary: TaskContextSummary | None,
        *,
        conversation_id: str = "",
        current_message_id: str | None = None,
        attached_file_ids: list[str] | None = None,
        exclude_id: int | None = None,
    ) -> IntentContext:
        """Build a bounded IntentContext from raw materials."""
        recent = self._pick_recent_messages(
            messages, self.max_intent_turns, exclude_id=exclude_id
        )
        truncated_summary = self._truncate_text(
            summary, self.max_intent_summary_chars
        )
        limited_files = file_summaries[: self.max_file_summaries]

        history = [
            ChatHistoryMessage(
                role="assistant" if m.role == "agent" else m.role,
                content=self._truncate_text(
                    m.content, self.max_single_message_chars
                ),
                message_id=getattr(m, "public_id", None),
                created_at=getattr(m, "created_at", None),
            )
            for m in recent
        ]

        total_tokens = self._estimate_context_tokens(
            truncated_summary, history, limited_files, task_summary
        )

        return IntentContext(
            conversation_id=conversation_id,
            current_message_id=current_message_id,
            recent_turns=history,
            conversation_summary=truncated_summary,
            file_summaries=limited_files,
            latest_task_summary=task_summary,
            attached_file_ids=list(attached_file_ids or []),
            estimated_tokens=total_tokens,
        )

    # ── Internals ────────────────────────────────────────────────

    def _pick_recent_messages(
        self,
        messages: list[Any],
        max_turns: int,
        *,
        exclude_id: int | None = None,
    ) -> list[Any]:
        """Pick the most recent N user/agent turns, ordered oldest-first."""
        max_messages = max_turns * 2  # each turn = user + agent
        # Filter: non-empty content, user/agent role, exclude specified id
        valid = []
        for m in messages:
            if exclude_id is not None and getattr(m, "id", None) == exclude_id:
                continue
            role = getattr(m, "role", "")
            msg_type = getattr(m, "message_type", "")
            content = getattr(m, "content", "") or ""
            if not content.strip():
                continue
            if role not in ("user", "agent"):
                continue
            if msg_type not in ("user_text", "agent_text"):
                continue
            valid.append(m)

        # Sort by created_at DESC, take top N, then reverse to ASC
        valid.sort(key=lambda m: getattr(m, "created_at", ""), reverse=True)
        selected = valid[:max_messages]
        selected.reverse()
        return selected

    @staticmethod
    def _truncate_text(text: str | None, max_chars: int) -> str | None:
        if text is None:
            return None
        if len(text) <= max_chars:
            return text
        # Leave room for the suffix
        cut_point = max_chars - len(_TRUNCATION_SUFFIX)
        if cut_point <= 0:
            return text[:max_chars]
        return text[:cut_point] + _TRUNCATION_SUFFIX

    @staticmethod
    def _estimate_context_tokens(
        summary: str | None,
        history: list[ChatHistoryMessage],
        file_summaries: list[FileContextSummary],
        task_summary: TaskContextSummary | None,
    ) -> int:
        total = 0
        if summary:
            total += estimate_tokens(summary)
        for msg in history:
            total += estimate_tokens(msg.content)
        for f in file_summaries:
            total += estimate_tokens(
                f"{f.file_name} {f.file_type or ''} {f.upload_status or ''}"
            )
        if task_summary:
            total += estimate_tokens(
                f"{task_summary.status} {task_summary.summary_text or ''}"
            )
        return total


# 模块定位:Context Reducer — 通过轮次 + token 上限压缩上下文
#
# 纯函数,**无 DB 依赖**:输入 ORM Message 对象 + schema,产出受限的
# ChatContext / IntentContext。
#
# 链路:
#   ConversationContextService.build_chat_context(conversation, user)
#     → reducer.reduce(messages, summary, files, max_tokens)
#       → 切尾 + 保留 system prompt 与最近 N 轮
#
# 关键约束:
#   - 纯函数,无副作用(便于 unit test);
#   - 跟 token_estimator 估算交互,不依赖 LLM 真 token 数;
#   - reducer.reduce 不能丢 system prompt(safety),只丢历史 user/assistant;
#   - 调用方负责传入 max_tokens(由 model_configs 决定,本模块不读配置)。
