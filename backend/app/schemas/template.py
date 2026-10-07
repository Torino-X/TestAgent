"""Public request contracts for the template marketplace."""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, Field


class TemplateUseRequest(BaseModel):
    conversation_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("conversation_id", "conversationId"),
    )

    model_config = {"populate_by_name": True}
