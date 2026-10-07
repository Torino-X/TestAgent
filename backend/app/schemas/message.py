"""Message schemas."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import AliasChoices, BaseModel, Field

from app.schemas.conversation import ConversationSummary


class SendMessageRequest(BaseModel):
    content: str = Field(..., description="User message text")
    attached_file_ids: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("attached_file_ids", "file_ids"),
        description="Files attached to this user message",
    )
    knowledge_mode_snapshot: str = Field(
        default="AUTO",
        description="Conversation-scoped knowledge mode at send time: AUTO | MAAS_STRICT",
    )

    model_config = {"populate_by_name": True}


class MessageDetail(BaseModel):
    message_id: str = Field(default="")
    conversation_id: str = ""
    role: str = "user"
    message_type: str = "user_text"
    content: Optional[str] = None
    payload: Optional[Dict[str, Any]] = Field(default=None, alias="payload_json")
    timestamp: str = ""

    # ── F013: routing metadata surfaced on assistant messages ────
    route: str = Field(default="", description="chat_reply | agent_task | ask_for_files | unsupported | clarify")
    intent: str = Field(default="", description="Recognized intent (IntentType enum value)")
    intent_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    # ── Phase 2.9A.26+: per-user feedback ────────────────────────
    # Only populated for agent_text rows.  Surface mode: the front-end
    # uses ``my_feedback`` to flip the like/dislike button without an
    # extra round-trip.
    my_feedback: Optional[str] = Field(default=None, description="like | dislike | null")

    model_config = {"from_attributes": True, "populate_by_name": True}


class FeedbackRequest(BaseModel):
    """Request body for ``PUT /api/messages/{public_id}/feedback``.

    ``feedback`` is a string from {``like``, ``dislike``, ``None``}.
    ``None`` cancels an existing reaction.

    Phase 3 will add an optional ``generation_id`` field once
    ``assistant_message_generations`` is in place; left out for now so
    the schema doesn't ship a forward-only field.
    """

    feedback: Optional[str] = Field(
        default=None,
        description="like | dislike | null (null cancels)",
    )

    model_config = {"populate_by_name": True}


class FeedbackResponse(BaseModel):
    """Response body for PUT feedback.

    Mirrors the natural key shape the front-end already knows, so the
    UI can reconcile its optimistic state without re-fetching messages.
    """

    message_id: str
    feedback: Optional[str] = Field(default=None, description="like | dislike | null")


class AgentTaskRef(BaseModel):
    task_id: str
    task_type: str = "test_plan_generation"
    status: str = "created"
    events_url: str = ""


class AgentReply(BaseModel):
    message_type: str = "agent_text"
    content: str = ""


class SendMessageResponse(BaseModel):
    message: MessageDetail
    agent_task: Optional[AgentTaskRef] = None
    agent_reply: Optional[AgentReply] = None
    conversation: Optional[ConversationSummary] = Field(
        default=None,
        description="Updated conversation summary when the first user message generated a title",
    )

    # ── F013: top-level routing fields (all optional / defaulted) ─
    # New top-level fields are populated for every response.  Frontend
    # can read these to decide whether to open an SSE stream, render an
    # inline assistant message, or show an "unsupported capability"
    # notice.  All fields default to "" / False / None so existing
    # callers that ignore them remain backward-compatible.
    route: str = Field(default="", description="Same value as MessageDetail.route")
    intent: str = Field(default="", description="Same value as MessageDetail.intent")
    requires_sse: bool = Field(default=False, description="True iff route == agent_task")
    task_id: Optional[str] = Field(
        default=None,
        description="Convenience alias of agent_task.task_id (None for non-task routes)",
    )
# schemas.message:Message 收发 Pydantic 契约(MessageCreate / MessageOut / 含附件 / 含 feedback);API 主链路契约。
