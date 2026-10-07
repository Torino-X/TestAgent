"""Context schemas for F016 layered context architecture.

These Pydantic models describe the structured context objects that
ConversationContextService builds and passes to ChatLLMService,
IntentRouter, and ContextSnapshotService.

KnowledgeContext and LongTermMemoryContext are reserved for future
use (real knowledge-base QA and long-term memory).  They are not
wired into the current pipeline.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ── 1. Chat history message ──────────────────────────────────────


class ChatHistoryMessage(BaseModel):
    """A single user or assistant turn used as context."""

    role: Literal["user", "assistant"]
    content: str
    message_id: str | None = None
    created_at: datetime | None = None


# ── 2. File context summary ──────────────────────────────────────


class FileContextSummary(BaseModel):
    """Lightweight file metadata for context — no storage_path or full text."""

    file_id: str
    file_name: str
    file_type: str | None = None
    confirmed_type: str | None = None
    upload_status: str | None = None
    parse_status: str | None = None
    description: str | None = None


# ── 3. Task context summary ──────────────────────────────────────


class TaskContextSummary(BaseModel):
    """Summary of the most recent AgentTask in the conversation."""

    task_id: str
    task_type: str | None = None
    status: str
    current_node: str | None = None
    resume_node: str | None = None
    latest_event_type: str | None = None
    pending_confirmation_count: int = 0
    artifact_count: int = 0
    latest_artifact_public_id: str | None = None
    latest_artifact_file_name: str | None = None
    latest_artifact_version_no: int | None = None
    failed_tool_name: str | None = None
    summary_text: str | None = None


# ── 4. Chat context ─────────────────────────────────────────────


class ChatContext(BaseModel):
    """Full context for a chat reply call."""

    conversation_id: str
    current_message_id: str | None = None
    conversation_summary: str | None = None
    recent_messages: list[ChatHistoryMessage] = Field(default_factory=list)
    file_summaries: list[FileContextSummary] = Field(default_factory=list)
    latest_task_summary: TaskContextSummary | None = None
    estimated_tokens: int = 0
    # F026: Knowledge-base snippets to inject as 【知识库参考】 when present.
    knowledge_snippets: list[dict] | None = None
    knowledge_source_count: int = 0
    # Phase 4: resolved Project context; absent for standalone conversations.
    project_context: dict[str, Any] | None = None


# ── 5. Intent context ───────────────────────────────────────────


class IntentContext(BaseModel):
    """Context for intent recognition — lighter than ChatContext."""

    conversation_id: str
    current_message_id: str | None = None
    recent_turns: list[ChatHistoryMessage] = Field(default_factory=list)
    conversation_summary: str | None = None
    file_summaries: list[FileContextSummary] = Field(default_factory=list)
    latest_task_summary: TaskContextSummary | None = None
    attached_file_ids: list[str] = Field(default_factory=list)
    estimated_tokens: int = 0


# ── 6. Task trigger context ─────────────────────────────────────


class TaskTriggerContext(BaseModel):
    """Recorded when an AgentTask is created — stored in input_payload."""

    conversation_id: str
    trigger_message_id: str
    user_goal: str
    recent_conversation_summary: str | None = None
    selected_file_ids: list[str] = Field(default_factory=list)
    latest_task_id: str | None = None
    intent: str | None = None
    route: str | None = None
    project_id: str | None = None
    project_context: dict[str, Any] | None = None


# ── 7. Knowledge context (reserved) ─────────────────────────────


class KnowledgeContext(BaseModel):
    """Reserved for future knowledge-base QA integration."""

    query: str
    snippets: list[dict] = Field(default_factory=list)
    source_count: int = 0


# ── 8. Long-term memory context (reserved) ──────────────────────


class LongTermMemoryContext(BaseModel):
    """Reserved for future long-term memory integration."""

    memories: list[dict] = Field(default_factory=list)
    source: str | None = None
