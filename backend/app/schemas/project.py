"""Project API request schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=5000)
    memory_mode: str = Field(default="project_memory", alias="memoryMode")


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=5000)
    memory_mode: str | None = Field(default=None, alias="memoryMode")
    instructions: str | None = Field(default=None, max_length=12000)


class ProjectConversationCreate(BaseModel):
    title: str = Field(default="新对话", max_length=255)


class ConversationProjectMove(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(alias="projectId")


class ProjectSourceAttach(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    file_id: str = Field(alias="fileId")
    source_role: str = Field(default="other", alias="sourceRole")


class ProjectSourceUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source_role: str | None = Field(default=None, alias="sourceRole")
    is_current: bool | None = Field(default=None, alias="isCurrent")


class ProjectInstructionsUpdate(BaseModel):
    instructions: str = Field(default="", max_length=12000)
