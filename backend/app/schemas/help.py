"""Public schemas for the Markdown-backed Help Center."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HelpSite(BaseModel):
    title: str
    description: str


class HelpArticleSummary(BaseModel):
    slug: str
    title: str
    description: str
    order: int
    keywords: list[str] = Field(default_factory=list)
    featured: bool = False
    reading_time_minutes: int = 1
    updated_at: str | None = None


class HelpSection(BaseModel):
    id: str
    title: str
    order: int
    articles: list[HelpArticleSummary] = Field(default_factory=list)


class HelpCatalog(BaseModel):
    site: HelpSite
    sections: list[HelpSection] = Field(default_factory=list)


class HelpSectionReference(BaseModel):
    id: str
    title: str


class HelpHeading(BaseModel):
    level: int
    text: str
    anchor: str


class HelpAdjacentArticle(BaseModel):
    slug: str
    title: str


class HelpArticle(BaseModel):
    slug: str
    title: str
    description: str
    section: HelpSectionReference
    markdown: str
    headings: list[HelpHeading] = Field(default_factory=list)
    reading_time_minutes: int = 1
    updated_at: str | None = None
    previous: HelpAdjacentArticle | None = None
    next: HelpAdjacentArticle | None = None


class HelpSearchItem(BaseModel):
    slug: str
    title: str
    section_id: str
    section_title: str
    description: str
    snippet: str
    score: int


class HelpSearchResult(BaseModel):
    query: str
    items: list[HelpSearchItem] = Field(default_factory=list)


class HelpAsset(BaseModel):
    path: str
    media_type: str

