"""Version-controlled Markdown catalogue for trusted system instructions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


_DEFAULT_RULES = "你是 TestAgent，一名面向软件测试工作的对话式 AI 助手。"
_INSTRUCTION_DIR = Path(__file__).resolve().parents[1] / "system_instructions"


@dataclass(frozen=True)
class SystemInstructionDocument:
    key: str
    version: str
    priority: int
    applies_to: tuple[str, ...]
    content: str

    def applies(self, call_site: str) -> bool:
        return "*" in self.applies_to or call_site in self.applies_to


def load_system_instruction_documents() -> list[SystemInstructionDocument]:
    """Load only repository-controlled Markdown files; fail closed to defaults."""
    if not _INSTRUCTION_DIR.is_dir():
        return []
    documents: list[SystemInstructionDocument] = []
    for path in sorted(_INSTRUCTION_DIR.glob("*.md")):
        try:
            document = _parse_document(path.read_text(encoding="utf-8"), fallback_key=path.stem)
        except OSError:
            continue
        if document is not None:
            documents.append(document)
    return sorted(documents, key=lambda item: (item.priority, item.key))


def render_system_instructions(call_site: str | None = None) -> str:
    site = (call_site or "chat.reply").strip() or "chat.reply"
    documents = [doc for doc in load_system_instruction_documents() if doc.applies(site)]
    if not documents:
        return _DEFAULT_RULES
    return "\n\n".join(
        f"[系统规则 {doc.key}@{doc.version}]\n{doc.content}" for doc in documents
    )


def system_instruction_manifest(call_site: str | None = None) -> list[dict[str, str]]:
    site = (call_site or "chat.reply").strip() or "chat.reply"
    return [
        {"type": "markdown_system_instruction", "id": doc.key, "version": doc.version}
        for doc in load_system_instruction_documents()
        if doc.applies(site)
    ]


def _parse_document(raw: str, *, fallback_key: str) -> SystemInstructionDocument | None:
    text = raw.strip()
    if not text:
        return None
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            for line in text[4:end].strip().splitlines():
                key, sep, value = line.partition(":")
                if sep:
                    meta[key.strip()] = value.strip()
            body = text[end + 4 :].strip()
    if not body:
        return None
    try:
        priority = int(meta.get("priority", "50"))
    except ValueError:
        priority = 50
    applies_to = tuple(
        part.strip() for part in meta.get("applies_to", "*").split(",") if part.strip()
    ) or ("*",)
    return SystemInstructionDocument(
        key=meta.get("key", fallback_key),
        version=meta.get("version", "v1"),
        priority=priority,
        applies_to=applies_to,
        content=body,
    )


__all__ = [
    "load_system_instruction_documents",
    "render_system_instructions",
    "system_instruction_manifest",
]
