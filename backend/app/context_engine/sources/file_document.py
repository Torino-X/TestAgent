"""File/document source adapter backed by uploaded file metadata.

This adapter intentionally keeps the ContextKind contract unchanged
(`EVIDENCE`) while using FileSemanticProfile metadata instead of the
obsolete requirement/template/attachment/document file_type enum.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.context_engine.models.context import ContextItem, ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind, ContextTrust, SourceType
from app.context_engine.models.source import ContextWarning, SourceCollectResult


class FileDocumentSourceAdapter:
    """Collect uploaded file summaries for context assembly."""

    source_kind = ContextKind.EVIDENCE

    def __init__(self, *, token_counter=None, max_metadata_chars: int = 2000) -> None:
        self._token_counter = token_counter
        self._max_metadata_chars = max_metadata_chars

    async def collect(
        self,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        *,
        runtime_context,
    ) -> SourceCollectResult:
        started = _now_ms()
        if request.call_site == "document.qa":
            return SourceCollectResult(
                adapter_key="file_document",
                kind=self.source_kind,
                attempted=False,
                latency_ms=_now_ms() - started,
            )
        conversation_id = request.conversation_id
        if not conversation_id:
            return SourceCollectResult(
                adapter_key="file_document",
                kind=self.source_kind,
                attempted=False,
                degraded=True,
                failure_code="context.source.no_conversation",
                warnings=[
                    ContextWarning(
                        code="context.source.no_conversation",
                        detail="conversation_id missing; skipped file source",
                        adapter_key="file_document",
                    )
                ],
                latency_ms=_now_ms() - started,
            )

        items: list[ContextItem] = []
        warnings: list[ContextWarning] = []
        try:
            async with runtime_context.session_factory() as session:
                from app.context_engine.sources._helpers import user_internal_id
                from app.repositories.attachment_understanding_repository import FileSemanticProfileRepository
                from app.repositories.file_repository import FileRepository

                repo = FileRepository(session)
                profile_repo = FileSemanticProfileRepository(session)
                files = await repo.list_by_conversation(
                    user_internal_id(runtime_context, request),
                    int(conversation_id),
                    limit=50,
                )
                for uploaded_file in files:
                    profile = await profile_repo.get_by_file_id(uploaded_file.id)
                    metadata = _profile_metadata(uploaded_file, profile)
                    rendered = _render_file_metadata(uploaded_file, metadata)
                    content = rendered[: self._max_metadata_chars]
                    items.append(
                        ContextItem(
                            item_id=f"file:{uploaded_file.public_id}",
                            kind=ContextKind.EVIDENCE,
                            source_type=SourceType.FILE_SUMMARY,
                            source_ref=uploaded_file.public_id,
                            title=uploaded_file.original_name,
                            content=content,
                            authority=55,
                            priority=5,
                            estimated_tokens=self._estimate(content),
                            trust=ContextTrust.BUSINESS_EVIDENCE,
                            metadata=metadata,
                        )
                    )
        except Exception:
            warnings.append(
                ContextWarning(
                    code="context.source.file_error",
                    detail="file source collection failed; degraded",
                    adapter_key="file_document",
                )
            )
            return SourceCollectResult(
                adapter_key="file_document",
                kind=self.source_kind,
                items=[],
                warnings=warnings,
                attempted=True,
                degraded=True,
                failure_code="context.source.file_error",
                latency_ms=_now_ms() - started,
            )

        return SourceCollectResult(
            adapter_key="file_document",
            kind=self.source_kind,
            items=items,
            warnings=warnings,
            attempted=True,
            degraded=False,
            latency_ms=_now_ms() - started,
        )

    def _estimate(self, text: str) -> int:
        if self._token_counter is not None:
            return self._token_counter.estimate(text).tokens
        return max(1, len(text) // 3)


def _profile_metadata(uploaded_file, profile) -> dict:
    document_kind = "unknown"
    semantic_labels: list = []
    profile_confidence = None
    profile_version = ""
    evidence_contract_version = ""
    profile_source_hash = ""
    if profile is not None and profile.status == "ready":
        document_kind = profile.document_kind or "unknown"
        semantic_labels = list(profile.semantic_labels_json or [])
        profile_confidence = float(profile.confidence) if profile.confidence is not None else None
        profile_version = profile.classifier_version or ""
        profile_source_hash = profile.source_hash or ""
        characteristics = profile.characteristics_json or {}
        evidence_contract_version = str(
            characteristics.get("evidence_contract_version") or ""
        )
    return {
        "file_public_id": uploaded_file.public_id,
        "document_kind": document_kind,
        "semantic_labels": semantic_labels,
        "profile_confidence": profile_confidence,
        "profile_version": profile_version,
        "evidence_contract_version": evidence_contract_version,
        "profile_source_hash": profile_source_hash,
        "mime_type": uploaded_file.mime_type,
        "file_size": uploaded_file.file_size,
        "file_hash": uploaded_file.file_hash,
        "task_id": uploaded_file.task_id,
    }


def _render_file_metadata(uploaded_file, metadata: dict) -> str:
    labels = ", ".join(str(label) for label in metadata.get("semantic_labels") or []) or "n/a"
    return "\n".join(
        [
            f"File: {uploaded_file.original_name} ({uploaded_file.file_ext}, {uploaded_file.file_size} bytes)",
            f"Semantic kind: {metadata.get('document_kind') or 'unknown'}",
            f"Labels: {labels}",
            f"MIME: {uploaded_file.mime_type or 'unknown'}",
            f"Hash: {uploaded_file.file_hash or 'n/a'}",
        ]
    )


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
# auto-appended module-level note: file_document source adapter。
# source adapter(file_document):把上传文件元数据(FileSemanticProfile)聚合到 ContextKind.EVIDENCE 通道;替代过时的 file_type 枚举。
