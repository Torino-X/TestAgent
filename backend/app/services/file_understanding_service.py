"""Automatic semantic profiling for uploaded files."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.document_reader import DocumentReadError, DocumentReader
from app.core.exceptions import NotFoundError
from app.llm.task_profiles import FILE_UNDERSTANDING_PROFILE
from app.models.attachment_understanding import FileSemanticProfile
from app.models.uploaded_file import UploadedFile
from app.repositories.attachment_understanding_repository import FileSemanticProfileRepository
from app.repositories.file_repository import FileRepository
from app.services.file_capability_registry import FileProcessingCapabilityRegistry
from app.utils.datetime import utcnow

logger = logging.getLogger(__name__)

CLASSIFIER_VERSION = "file_understanding:v1"
EVIDENCE_CONTRACT_VERSION = "file_semantic_evidence:v1"
MAX_SAMPLE_CHARS = 6000
MAX_STORED_SUMMARY_CHARS = 1000

_ALLOWED_DOCUMENT_KINDS = {
    "requirements_specification",
    "test_plan_template",
    "supplemental_reference",
    "unknown",
}


class FileUnderstandingClassifier(Protocol):
    async def classify(self, sample: dict[str, Any]) -> dict[str, Any]:
        ...


class ContextEngineFileUnderstandingClassifier:
    """Semantic classifier whose prompt is composed and audited by Context Engine."""

    def __init__(self, *, bridge: Any, llm_client: Any, user_id: int, session_factory: Any) -> None:
        self._bridge = bridge
        self._llm_client = llm_client
        self._user_id = user_id
        self._session_factory = session_factory

    async def classify(self, sample: dict[str, Any]) -> dict[str, Any]:
        from types import SimpleNamespace

        payload = json.dumps(sample, ensure_ascii=False)
        result = await self._bridge.generate(
            user_id=self._user_id,
            call_site="file.understanding",
            llm_task_profile=FILE_UNDERSTANDING_PROFILE,
            current_goal=payload,
            user_content=payload,
            output_contract="json",
            runtime_context=SimpleNamespace(
                session_factory=self._session_factory,
                llm_client=self._llm_client,
                user_internal_id=self._user_id,
            ),
        )
        value = getattr(result, "value", None) if result is not None else None
        if not isinstance(value, dict):
            raise FileUnderstandingError("context_engine_profile_parse_failed")
        return value


class FileUnderstandingError(Exception):
    """Raised for recoverable file understanding failures."""


class FileSemanticSampler:
    """Build bounded, sanitized samples for semantic classification."""

    def __init__(self, reader: DocumentReader | None = None) -> None:
        self._reader = reader or DocumentReader()

    def build(self, uploaded_file: UploadedFile, *, path: Path | None = None) -> dict[str, Any]:
        text, stats = self._read_text(uploaded_file, path=path)
        sanitized = _sanitize_text(text)
        headings = _extract_headings(sanitized)
        table_headers = _extract_table_headers(sanitized)
        excerpts = _representative_excerpts(sanitized, MAX_SAMPLE_CHARS)
        sample_text = "\n\n".join(excerpts)
        return {
            "metadata": {
                "filename": uploaded_file.original_name,
                "extension": uploaded_file.file_ext,
                "mime_type": uploaded_file.mime_type,
                "file_size": uploaded_file.file_size,
            },
            "structure": {
                "title": headings[0] if headings else _stem_title(uploaded_file.original_name),
                "headings": headings[:20],
                "table_headers": table_headers[:20],
                "statistics": stats | {
                    "sanitized_char_count": len(sanitized),
                    "sample_char_count": len(sample_text),
                    "heading_count": len(headings),
                    "table_header_count": len(table_headers),
                },
            },
            "representative_excerpts": excerpts,
        }

    def _read_text(
        self, uploaded_file: UploadedFile, *, path: Path | None = None
    ) -> tuple[str, dict[str, int | str]]:
        path = path or _resolve_storage_path(uploaded_file.storage_path)
        ext = (uploaded_file.file_ext or path.suffix.lstrip(".")).lower()
        if not path.exists():
            raise FileUnderstandingError("file_missing")
        try:
            if ext in {"docx", "txt", "md"}:
                result = self._reader.read_structured(str(path))
                text = result.to_prompt_text()
                return text, {
                    "block_count": len(result.blocks),
                    "image_count": len(result.image_blocks),
                    "char_count": len(text),
                    "line_count": len(text.splitlines()),
                }
            if ext == "json":
                text = path.read_text(encoding="utf-8")
                return text, {
                    "block_count": 1,
                    "image_count": 0,
                    "char_count": len(text),
                    "line_count": len(text.splitlines()),
                }
        except (DocumentReadError, UnicodeDecodeError, OSError) as exc:
            raise FileUnderstandingError("read_failed") from exc
        raise FileUnderstandingError("unsupported_file_type")


class FileUnderstandingService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        classifier: FileUnderstandingClassifier | Any | None = None,
        sampler: FileSemanticSampler | None = None,
        classifier_version: str = CLASSIFIER_VERSION,
        context_llm_invoker: Any | None = None,
    ) -> None:
        self._session = session
        self._file_repo = FileRepository(session)
        self._profile_repo = FileSemanticProfileRepository(session)
        self._classifier = classifier
        self._sampler = sampler or FileSemanticSampler()
        self._classifier_version = classifier_version
        self._context_llm_invoker = context_llm_invoker

    async def understand_file(
        self,
        *,
        user_internal_id: int,
        file_public_id: str,
    ) -> FileSemanticProfile:
        uploaded = await self._file_repo.get_by_public_id(user_internal_id, file_public_id)
        if uploaded is None:
            raise NotFoundError("file")

        source_hash = await _source_hash_for(uploaded)
        existing = await self._profile_repo.get_by_file_id(uploaded.id)
        if (
            existing is not None
            and existing.status == "ready"
            and existing.source_hash == source_hash
            and existing.classifier_version == self._classifier_version
        ):
            await self._sync_index_metadata(user_internal_id, file_public_id)
            return existing

        capability = FileProcessingCapabilityRegistry().for_extension(uploaded.file_ext)
        if not capability.can_semantic_profile:
            return await self._profile_repo.upsert_profile(
                uploaded_file=uploaded,
                status="unsupported",
                document_kind="unknown",
                characteristics={"extension": uploaded.file_ext},
                classifier_version=self._classifier_version,
                source_hash=source_hash,
                error_code="unsupported_file_type",
            )

        profile = await self._profile_repo.upsert_profile(
            uploaded_file=uploaded,
            status="processing",
            document_kind="unknown",
            characteristics={
                "started_at": utcnow().isoformat(),
                "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
                "external_model_route": "file_semantic_classifier",
            },
            classifier_version=self._classifier_version,
            source_hash=source_hash,
        )
        try:
            if uploaded.storage_type == "oss":
                from app.storage.oss_storage import object_storage

                suffix = f".{uploaded.file_ext.lstrip('.')}" if uploaded.file_ext else ""
                async with object_storage.stage_file(uploaded.storage_path, suffix=suffix) as path:
                    sample = self._sampler.build(uploaded, path=path)
            else:
                sample = self._sampler.build(uploaded)
            logger.info(
                "EXTERNAL_MODEL_BOUNDARY | component=file_understanding | "
                "operation=semantic_profile | contract=%s | user_id=%s | "
                "file_id=%s | source_hash=%s | input_chars=%s",
                EVIDENCE_CONTRACT_VERSION,
                user_internal_id,
                uploaded.public_id,
                source_hash[:24],
                sample.get("structure", {}).get("statistics", {}).get("sample_char_count", 0),
            )
            classified = await self._classify(sample, user_internal_id=user_internal_id)
            normalized = _normalize_classification(classified)
            profile = await self._profile_repo.upsert_profile(
                uploaded_file=uploaded,
                status="ready",
                document_kind=normalized["document_kind"],
                summary=normalized["summary"],
                semantic_labels=normalized["semantic_labels"],
                possible_usages=normalized["possible_usages"],
                characteristics={
                    **_characteristics_from_sample(sample),
                    "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
                    "external_model_route": "file_semantic_classifier",
                    "source_hash_prefix": source_hash[:24],
                },
                confidence=normalized["confidence"],
                classifier_version=self._classifier_version,
                source_hash=source_hash,
            )
            await self._sync_index_metadata(user_internal_id, file_public_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "EXTERNAL_MODEL_BOUNDARY | component=file_understanding | "
                "operation=semantic_profile | contract=%s | status=failed | "
                "file=%s | err_type=%s | err=%s",
                EVIDENCE_CONTRACT_VERSION,
                uploaded.public_id,
                type(exc).__name__,
                str(exc)[:200],
            )
            profile = await self._profile_repo.upsert_profile(
                uploaded_file=uploaded,
                status="failed",
                document_kind="unknown",
                summary=None,
                semantic_labels=[],
                possible_usages=[],
                characteristics={
                    "failed_at": utcnow().isoformat(),
                    "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
                    "external_model_route": "file_semantic_classifier",
                    "source_hash_prefix": source_hash[:24],
                },
                confidence=0.0,
                classifier_version=self._classifier_version,
                source_hash=source_hash,
                error_code="classification_failed",
            )
        return profile

    async def _sync_index_metadata(self, user_internal_id: int, file_public_id: str) -> None:
        try:
            from app.context_engine.indexing.document_service import IndexDocumentService

            await IndexDocumentService(self._session).sync_uploaded_file_profile_metadata(
                user_id=user_internal_id,
                file_public_id=file_public_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "file understanding index metadata sync failed | file=%s | err_type=%s | err=%s",
                file_public_id,
                type(exc).__name__,
                str(exc)[:200],
            )

    async def _classify(self, sample: dict[str, Any], *, user_internal_id: int) -> dict[str, Any]:
        classifier = self._classifier
        if classifier is None:
            bridge = self._context_llm_invoker
            if bridge is None or not getattr(bridge, "available", False):
                raise FileUnderstandingError("context_engine_unavailable")
            from app.services.settings_service import SettingsService
            from app.db.session import AsyncSessionLocal
            from app.integrations.llm_client import LLMClient

            provider = await SettingsService(self._session).build_llm_config_provider(user_internal_id)
            classifier = ContextEngineFileUnderstandingClassifier(
                bridge=bridge,
                llm_client=LLMClient(config_provider=provider),
                user_id=user_internal_id,
                session_factory=AsyncSessionLocal,
            )

        if hasattr(classifier, "classify"):
            result = classifier.classify(sample)
        else:
            result = classifier(sample)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise FileUnderstandingError("classifier_returned_non_object")
        return result


def _source_hash(uploaded_file: UploadedFile) -> str:
    if uploaded_file.file_hash:
        return f"file_hash:{uploaded_file.file_hash}"
    path = _resolve_storage_path(uploaded_file.storage_path)
    if path.exists():
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    fallback = f"{uploaded_file.public_id}:{uploaded_file.file_size}:{uploaded_file.updated_at}"
    return "metadata:" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()


async def _source_hash_for(uploaded_file: UploadedFile) -> str:
    if uploaded_file.file_hash:
        return f"file_hash:{uploaded_file.file_hash}"
    if uploaded_file.storage_type == "oss":
        from app.storage.oss_storage import object_storage

        try:
            content = await object_storage.read_bytes(uploaded_file.storage_path)
        except FileNotFoundError:
            content = b""
        if content:
            return "sha256:" + hashlib.sha256(content).hexdigest()
    return _source_hash(uploaded_file)


def _resolve_storage_path(storage_path: str | Path) -> Path:
    from app.storage.local_storage import local_storage

    raw = str(storage_path)
    path = Path(raw)
    if path.is_absolute() and not raw.startswith(("/", "\\")):
        return path

    full_path = local_storage._base / raw  # noqa: SLF001
    try:
        full_path.resolve().relative_to(local_storage._base.resolve())  # noqa: SLF001
    except ValueError as exc:
        raise FileUnderstandingError("path_traversal") from exc
    return full_path


def _normalize_classification(raw: dict[str, Any]) -> dict[str, Any]:
    document_kind = str(raw.get("document_kind") or "unknown")
    if document_kind not in _ALLOWED_DOCUMENT_KINDS:
        document_kind = "unknown"
    confidence = raw.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "document_kind": document_kind,
        "summary": _sanitize_text(str(raw.get("summary") or ""))[:MAX_STORED_SUMMARY_CHARS],
        "semantic_labels": _string_list(raw.get("semantic_labels")),
        "possible_usages": _string_list(raw.get("possible_usages")),
        "confidence": confidence,
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:20]:
        text = _sanitize_text(str(item)).strip()
        if text:
            result.append(text[:80])
    return result


def _characteristics_from_sample(sample: dict[str, Any]) -> dict[str, Any]:
    structure = sample.get("structure") if isinstance(sample.get("structure"), dict) else {}
    stats = structure.get("statistics") if isinstance(structure.get("statistics"), dict) else {}
    excerpts = sample.get("representative_excerpts")
    sample_text = ""
    if isinstance(excerpts, list):
        sample_text = "\n".join(str(item) for item in excerpts)
    return {
        "metadata": sample.get("metadata", {}),
        "title": structure.get("title"),
        "headings": structure.get("headings", [])[:20],
        "table_headers": structure.get("table_headers", [])[:20],
        "statistics": stats,
        "sample_digest": hashlib.sha256(sample_text.encode("utf-8")).hexdigest() if sample_text else None,
    }


def _sanitize_text(text: str) -> str:
    text = _redact_secrets(text or "")
    try:
        from app.context_engine.security.pii import PIIRedactor

        text = PIIRedactor(mode="redact").apply(text)
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.context_engine.security.injection import InjectionGuard

        text = InjectionGuard.sanitize(text)
    except Exception:  # noqa: BLE001
        pass
    return text


def _redact_secrets(text: str) -> str:
    patterns = [
        re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"),
        re.compile(r"(?i)bearer\s+[a-z0-9._\-]{16,}"),
        re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    ]
    result = text
    for pattern in patterns:
        result = pattern.sub("[secret_redacted]", result)
    return result


def _extract_headings(text: str) -> list[str]:
    headings: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("#").strip()
        if not line:
            continue
        if raw_line.lstrip().startswith("#") or len(line) <= 80 and re.search(r"(Requirement|Template|Scope|Criteria|Plan|Specification)", line, re.I):
            headings.append(line[:120])
    return headings


def _extract_table_headers(text: str) -> list[list[str]]:
    headers: list[list[str]] = []
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        cells = [cell for cell in cells if cell and not set(cell) <= {"-", ":"}]
        if len(cells) >= 2:
            headers.append(cells[:12])
    return headers


def _representative_excerpts(text: str, max_chars: int) -> list[str]:
    compact = re.sub(r"\n{3,}", "\n\n", text.strip())
    if not compact:
        return []
    if len(compact) <= max_chars:
        return [compact]
    slice_size = max_chars // 3
    middle_start = max(0, len(compact) // 2 - slice_size // 2)
    return [
        compact[:slice_size],
        compact[middle_start:middle_start + slice_size],
        compact[-slice_size:],
    ]


def _stem_title(filename: str) -> str:
    return Path(filename).stem[:120]


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (F019/F020 File Understanding Pipeline 编排):
#
#   链路:
#     文件上传 / 自动识别触发:
#       → FileUnderstandingService.understand_uploaded_file(public_id)
#         → 1. classify by extension/mime → file_capability;
#         → 2. image / image-embedded → ImageUnderstandingOrchestrator;
#         → 3. docx/pdf → DocumentReader → requirement_summary etc;
#         → 4. 把结果写 attachment_understanding + uploaded_file.file_type
#
# 关键约束(供开发者速查):
#   - 不允许在本服务直接调 vision/ocr — 走 orchestrator;
#   - 失败必须 swallow,落到 attachment_understanding.status='failed';
#   - 与 PHASE-1 attachment_backfill_service 同一根编排,后者仅一次性批量跑。
