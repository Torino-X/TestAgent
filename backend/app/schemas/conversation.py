"""Conversation schemas."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class ConversationCreate(BaseModel):
    title: str = "新会话"


class ConversationUpdate(BaseModel):
    title: Optional[str] = None


class ConversationSummary(BaseModel):
    id: str
    title: str
    state: str
    updated_at: str
    message_count: int = 0
    file_count: int = 0
    project_id: Optional[str] = None
    project_name: Optional[str] = None

    model_config = {"from_attributes": True}


class ConversationDetail(BaseModel):
    id: str
    title: str
    state: str
    updated_at: str
    message_count: int = 0
    file_count: int = 0
    files: List["FileSummary"] = []
    messages: List["MessageDetail"] = []

    model_config = {"from_attributes": True}


class ConversationListResponse(BaseModel):
    conversations: List[ConversationSummary]
    total: int
