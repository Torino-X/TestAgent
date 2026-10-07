"""Read-only Markdown Help Center catalog, article, search, and asset service."""

from __future__ import annotations

import logging
import mimetypes
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.schemas.help import (
    HelpAdjacentArticle,
    HelpArticle,
    HelpArticleSummary,
    HelpCatalog,
    HelpHeading,
    HelpSearchItem,
    HelpSearchResult,
    HelpSection,
    HelpSectionReference,
    HelpSite,
)

logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*#*\s*$")
_MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_DECORATION_RE = re.compile(r"[`*_~>#|]")
_WHITESPACE_RE = re.compile(r"\s+")
_ASSET_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class HelpContentError(RuntimeError):
    """Base class for controlled Help content failures."""


class HelpManifestError(HelpContentError):
    pass


class HelpArticleNotFound(HelpContentError):
    pass


class HelpAssetNotFound(HelpContentError):
    pass


class HelpSearchError(HelpContentError):
    pass


@dataclass(frozen=True)
class ResolvedHelpAsset:
    path: Path
    media_type: str


@dataclass(frozen=True)
class _ArticleRecord:
    slug: str
    title: str
    description: str
    file_path: Path
    order: int
    keywords: tuple[str, ...]
    featured: bool
    updated_at: str | None
    section_id: str
    section_title: str
    section_order: int
    markdown: str
    headings: tuple[HelpHeading, ...]
    reading_time_minutes: int
    plain_text: str


def _default_help_root() -> Path:
    return Path(__file__).resolve().parents[2] / "resources" / "help"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _plain_inline(value: str) -> str:
    value = _MARKDOWN_LINK_RE.sub(r"\1", value)
    value = _MARKDOWN_DECORATION_RE.sub("", value)
    return _WHITESPACE_RE.sub(" ", value).strip()


def heading_anchor(text: str, seen: dict[str, int] | None = None) -> str:
    """Build a stable Unicode-safe heading anchor shared with the frontend spec."""

    normalized = unicodedata.normalize("NFKC", _plain_inline(text)).strip().lower()
    pieces: list[str] = []
    pending_dash = False
    for char in normalized:
        if char.isspace() or char in {"-", "_"}:
            pending_dash = bool(pieces)
            continue
        category = unicodedata.category(char)
        if category.startswith(("L", "N")):
            if pending_dash and pieces and pieces[-1] != "-":
                pieces.append("-")
            pieces.append(char)
            pending_dash = False
    base = "".join(pieces).strip("-") or "section"
    if seen is None:
        return base
    count = seen.get(base, 0) + 1
    seen[base] = count
    return base if count == 1 else f"{base}-{count}"


class HelpCenterService:
    """Loads a validated static manifest lazily and serves immutable view models."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else _default_help_root()
        self.root = self.root.resolve()
        self.assets_root = (self.root / "assets").resolve()
        self._catalog: HelpCatalog | None = None
        self._records: dict[str, _ArticleRecord] = {}
        self._ordered: list[_ArticleRecord] = []

    def load_manifest(self) -> dict[str, Any]:
        path = self.root / "manifest.yaml"
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise HelpManifestError("help manifest does not exist") from exc
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise HelpManifestError(f"help manifest is not readable: {exc}") from exc
        if not isinstance(payload, dict):
            raise HelpManifestError("help manifest must be a mapping")
        return payload

    def validate_manifest(self, payload: dict[str, Any]) -> tuple[HelpCatalog, list[_ArticleRecord]]:
        if payload.get("schema_version") != 1:
            raise HelpManifestError("unsupported help manifest schema_version")
        site_payload = payload.get("site")
        sections_payload = payload.get("sections")
        if not isinstance(site_payload, dict) or not isinstance(sections_payload, list):
            raise HelpManifestError("help manifest requires site and sections")

        site = HelpSite(
            title=str(site_payload.get("title") or "").strip(),
            description=str(site_payload.get("description") or "").strip(),
        )
        if not site.title:
            raise HelpManifestError("help site title is required")

        section_ids: set[str] = set()
        slugs: set[str] = set()
        sections: list[HelpSection] = []
        records: list[_ArticleRecord] = []

        for section_payload in sections_payload:
            if not isinstance(section_payload, dict):
                raise HelpManifestError("help section must be a mapping")
            section_id = str(section_payload.get("id") or "").strip()
            section_title = str(section_payload.get("title") or "").strip()
            section_order = int(section_payload.get("order") or 0)
            if not section_id or not section_title:
                raise HelpManifestError("help section id and title are required")
            if section_id in section_ids:
                raise HelpManifestError(f"duplicate section id: {section_id}")
            section_ids.add(section_id)

            article_summaries: list[HelpArticleSummary] = []
            articles_payload = section_payload.get("articles") or []
            if not isinstance(articles_payload, list):
                raise HelpManifestError(f"articles must be a list: {section_id}")

            for article_payload in articles_payload:
                if not isinstance(article_payload, dict):
                    raise HelpManifestError("help article must be a mapping")
                slug = str(article_payload.get("slug") or "").strip()
                title = str(article_payload.get("title") or "").strip()
                description = str(article_payload.get("description") or "").strip()
                relative_file = str(article_payload.get("file") or "").strip()
                if not slug or not title or not relative_file:
                    raise HelpManifestError("help article slug, title, and file are required")
                if slug in slugs:
                    raise HelpManifestError(f"duplicate slug: {slug}")
                slugs.add(slug)

                article_path = (self.root / relative_file).resolve()
                if not _is_relative_to(article_path, self.root):
                    raise HelpManifestError(f"article path escapes help root: {relative_file}")
                if article_path.suffix.lower() != ".md":
                    raise HelpManifestError(f"article file must be a .md file: {relative_file}")
                if not article_path.is_file():
                    raise HelpManifestError(f"article file does not exist: {relative_file}")
                try:
                    markdown = article_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise HelpManifestError(f"article file is not readable: {relative_file}") from exc

                headings = tuple(self.extract_headings(markdown))
                reading_time = self.estimate_reading_time(markdown)
                keywords_value = article_payload.get("keywords") or []
                if not isinstance(keywords_value, list):
                    raise HelpManifestError(f"article keywords must be a list: {slug}")
                keywords = tuple(str(item).strip() for item in keywords_value if str(item).strip())
                order = int(article_payload.get("order") or 0)
                featured = bool(article_payload.get("featured", False))
                updated_at_value = article_payload.get("updated_at")
                updated_at = str(updated_at_value) if updated_at_value is not None else None
                record = _ArticleRecord(
                    slug=slug,
                    title=title,
                    description=description,
                    file_path=article_path,
                    order=order,
                    keywords=keywords,
                    featured=featured,
                    updated_at=updated_at,
                    section_id=section_id,
                    section_title=section_title,
                    section_order=section_order,
                    markdown=markdown,
                    headings=headings,
                    reading_time_minutes=reading_time,
                    plain_text=self.plain_text(markdown),
                )
                records.append(record)
                article_summaries.append(self._summary(record))

            article_summaries.sort(key=lambda item: (item.order, item.title))
            sections.append(
                HelpSection(
                    id=section_id,
                    title=section_title,
                    order=section_order,
                    articles=article_summaries,
                )
            )

        sections.sort(key=lambda item: (item.order, item.title))
        records.sort(key=lambda item: (item.section_order, item.order, item.title))
        return HelpCatalog(site=site, sections=sections), records

    def get_catalog(self) -> HelpCatalog:
        self._ensure_loaded()
        assert self._catalog is not None
        return self._catalog.model_copy(deep=True)

    def get_article(self, slug: str) -> HelpArticle:
        self._ensure_loaded()
        record = self._records.get(slug)
        if record is None:
            raise HelpArticleNotFound(f"help article not found: {slug}")
        index = self._ordered.index(record)
        previous = self._adjacent(self._ordered[index - 1]) if index > 0 else None
        next_article = self._adjacent(self._ordered[index + 1]) if index + 1 < len(self._ordered) else None
        return HelpArticle(
            slug=record.slug,
            title=record.title,
            description=record.description,
            section=HelpSectionReference(id=record.section_id, title=record.section_title),
            markdown=record.markdown,
            headings=list(record.headings),
            reading_time_minutes=record.reading_time_minutes,
            updated_at=record.updated_at,
            previous=previous,
            next=next_article,
        )

    def search(self, query: str, limit: int = 20) -> HelpSearchResult:
        self._ensure_loaded()
        cleaned = _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", query)).strip()
        if len(cleaned) > 100:
            raise HelpSearchError("help search query is too long")
        if not cleaned:
            return HelpSearchResult(query="", items=[])
        bounded_limit = max(1, min(int(limit), 50))
        normalized_query = cleaned.casefold()
        tokens = [part for part in normalized_query.split(" ") if part]
        items: list[HelpSearchItem] = []

        for record in self._ordered:
            title = record.title.casefold()
            description = record.description.casefold()
            keywords = " ".join(record.keywords).casefold()
            headings = " ".join(heading.text for heading in record.headings).casefold()
            body = record.plain_text.casefold()
            fields = (title, description, keywords, headings, body)
            if not all(any(token in field for field in fields) for token in tokens):
                continue

            score = 0
            if title == normalized_query:
                score += 100
            elif normalized_query in title:
                score += 60
            if normalized_query in keywords:
                score += 40
            if normalized_query in headings:
                score += 25
            if normalized_query in description:
                score += 20
            if normalized_query in body:
                score += 10
            if score == 0:
                score = 5 * len(tokens)
            items.append(
                HelpSearchItem(
                    slug=record.slug,
                    title=record.title,
                    section_id=record.section_id,
                    section_title=record.section_title,
                    description=record.description,
                    snippet=self._snippet(record, cleaned),
                    score=score,
                )
            )

        items.sort(key=lambda item: (-item.score, item.title))
        return HelpSearchResult(query=cleaned, items=items[:bounded_limit])

    def resolve_asset(self, asset_path: str) -> ResolvedHelpAsset:
        normalized = asset_path.replace("\\", "/").lstrip("/")
        candidate = (self.assets_root / normalized).resolve()
        if not normalized or not _is_relative_to(candidate, self.assets_root):
            raise HelpAssetNotFound("help asset not found")
        media_type = _ASSET_MEDIA_TYPES.get(candidate.suffix.lower())
        if media_type is None or not candidate.is_file():
            raise HelpAssetNotFound("help asset not found")
        return ResolvedHelpAsset(path=candidate, media_type=media_type)

    @staticmethod
    def extract_headings(markdown: str) -> list[HelpHeading]:
        headings: list[HelpHeading] = []
        seen: dict[str, int] = {}
        in_fence = False
        for line in markdown.replace("\r\n", "\n").split("\n"):
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            match = _HEADING_RE.match(line)
            if not match:
                continue
            text = _plain_inline(match.group(2))
            if text:
                headings.append(
                    HelpHeading(
                        level=len(match.group(1)),
                        text=text,
                        anchor=heading_anchor(text, seen),
                    )
                )
        return headings

    @staticmethod
    def plain_text(markdown: str) -> str:
        text = re.sub(r"```[\s\S]*?```", " ", markdown)
        text = _MARKDOWN_LINK_RE.sub(r"\1", text)
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^\s*:::\w+\s*$", "", text, flags=re.MULTILINE)
        text = text.replace(":::", " ")
        text = _MARKDOWN_DECORATION_RE.sub(" ", text)
        return _WHITESPACE_RE.sub(" ", text).strip()

    @classmethod
    def estimate_reading_time(cls, markdown: str) -> int:
        plain = cls.plain_text(markdown)
        chinese_chars = len(re.findall(r"[\u3400-\u9fff]", plain))
        latin_words = len(re.findall(r"[A-Za-z0-9]+", plain))
        units = chinese_chars + latin_words * 2
        return max(1, (units + 299) // 300)

    def _ensure_loaded(self) -> None:
        if self._catalog is not None:
            return
        catalog, records = self.validate_manifest(self.load_manifest())
        self._catalog = catalog
        self._ordered = records
        self._records = {record.slug: record for record in records}

    @staticmethod
    def _summary(record: _ArticleRecord) -> HelpArticleSummary:
        return HelpArticleSummary(
            slug=record.slug,
            title=record.title,
            description=record.description,
            order=record.order,
            keywords=list(record.keywords),
            featured=record.featured,
            reading_time_minutes=record.reading_time_minutes,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _adjacent(record: _ArticleRecord) -> HelpAdjacentArticle:
        return HelpAdjacentArticle(slug=record.slug, title=record.title)

    @staticmethod
    def _snippet(record: _ArticleRecord, query: str) -> str:
        source = record.description if query.casefold() in record.description.casefold() else record.plain_text
        lower = source.casefold()
        index = lower.find(query.casefold())
        if index < 0:
            return source[:140].strip()
        start = max(0, index - 44)
        end = min(len(source), index + len(query) + 88)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(source) else ""
        return f"{prefix}{source[start:end].strip()}{suffix}"

