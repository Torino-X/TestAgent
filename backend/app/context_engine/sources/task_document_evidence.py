"""Task-local parsed-document evidence for Context Engine assembly.

The test-plan graph already parses its requirement and template before the
generation LLM call.  This adapter makes that *bounded, task-local* parsed
state available through the normal EVIDENCE selection path.  It deliberately
does not read arbitrary files or bypass the profile/source-quota contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.common.token_estimator import estimate_tokens
from app.context_engine.models.context import ContextItem, ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind, ContextTrust, SourceType
from app.context_engine.models.source import SourceCollectResult


class TaskDocumentEvidenceSourceAdapter:
    """Expose parser-produced requirement/template data as untrusted evidence."""

    source_kind = ContextKind.EVIDENCE

    def __init__(self, *, token_counter=None) -> None:
        self._token_counter = token_counter

    async def collect(
        self,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        *,
        runtime_context,
    ) -> SourceCollectResult:
        del scope, runtime_context
        started = _now_ms()
        if request.call_site == "document.qa":
            return SourceCollectResult(
                adapter_key="task_document_evidence",
                kind=self.source_kind,
                attempted=False,
                latency_ms=_now_ms() - started,
            )
        evidence = _evidence_state(request.state_ref)
        if evidence is None:
            return SourceCollectResult(
                adapter_key="task_document_evidence",
                kind=self.source_kind,
                attempted=False,
                degraded=False,
                latency_ms=_now_ms() - started,
            )

        budget = max(0, int(section_plan.budget_tokens or 0))
        items: list[ContextItem] = []

        manifest = evidence.get("coverage_manifest")
        if isinstance(manifest, dict) and manifest.get("coverage_complete") is False:
            return _unprepared_result(started)

        for index, raw in enumerate(_records(evidence.get("parsed_documents"))):
            content = str(raw.get("content") or "").strip()
            if not content:
                continue
            items.append(
                _item(
                    item_id=f"parsed_document:{index}",
                    source_type=SourceType.PARSED_DOCUMENT,
                    source_ref=_source_ref(raw, request, "requirement"),
                    title=_title(raw, "parsed requirement"),
                    content=content,
                    priority=10,
                    estimate=self._estimate,
                    metadata=_record_metadata(raw),
                )
            )

        for index, raw in enumerate(_records(evidence.get("template_sections"))):
            content = str(raw.get("content") or "").strip()
            if not content:
                continue
            items.append(
                _item(
                    item_id=f"template_section:{index}",
                    source_type=SourceType.TEMPLATE_SECTION,
                    source_ref=_source_ref(raw, request, "template"),
                    title=_title(raw, "parsed template sections"),
                    content=content,
                    priority=9,
                    estimate=self._estimate,
                    metadata=_record_metadata(raw),
                )
            )

        for index, raw in enumerate(_records(evidence.get("generated_content"))):
            content = str(raw.get("content") or "").strip()
            if not content:
                continue
            items.append(
                _item(
                    item_id=f"generated_content:{index}",
                    source_type=SourceType.GENERATED_CONTENT,
                    source_ref=_source_ref(raw, request, "generated"),
                    title=_title(raw, "generated test-plan content"),
                    content=content,
                    priority=10,
                    estimate=self._estimate,
                    metadata=_record_metadata(raw),
                )
            )

        for index, raw in enumerate(_records(evidence.get("review_results"))):
            content = str(raw.get("content") or "").strip()
            if not content:
                continue
            items.append(
                _item(
                    item_id=f"review_result:{index}",
                    source_type=SourceType.REVIEW_RESULT,
                    source_ref=_source_ref(raw, request, "review"),
                    title=_title(raw, "review result"),
                    content=content,
                    priority=10,
                    estimate=self._estimate,
                    metadata=_record_metadata(raw),
                )
            )

        # Oversized evidence must arrive as an explicitly prepared, locked
        # full-text record or chunk-extraction bundle.  Silently cutting a
        # prefix here would make the omitted tail impossible to audit.
        if budget and sum(item.estimated_tokens for item in items) > budget:
            if not items or not all(item.metadata.get("locked") for item in items):
                return _unprepared_result(started)

        return SourceCollectResult(
            adapter_key="task_document_evidence",
            kind=self.source_kind,
            items=items,
            attempted=True,
            degraded=False,
            latency_ms=_now_ms() - started,
        )

    def _estimate(self, text: str) -> int:
        if self._token_counter is not None:
            return self._token_counter.estimate(text).tokens
        return estimate_tokens(text)


def _evidence_state(state_ref: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(state_ref, dict):
        return None
    value = state_ref.get("context_evidence")
    return value if isinstance(value, dict) else None


def _records(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _source_ref(raw: dict[str, Any], request: ContextRequest, role: str) -> str:
    source_ref = str(raw.get("source_ref") or "").strip()
    if source_ref:
        return source_ref[:256]
    return f"{request.task_id or 'task'}:{role}"


def _title(raw: dict[str, Any], fallback: str) -> str:
    return str(raw.get("title") or fallback).strip()[:255] or fallback


def _item(
    *,
    item_id: str,
    source_type: SourceType,
    source_ref: str,
    title: str,
    content: str,
    priority: int,
    estimate,
    metadata: dict[str, Any] | None = None,
) -> ContextItem:
    item_metadata = {"adapter_key": "task_document_evidence"}
    item_metadata.update(metadata or {})
    return ContextItem(
        item_id=item_id,
        kind=ContextKind.EVIDENCE,
        source_type=source_type,
        source_ref=source_ref,
        title=title,
        content=content,
        authority=80,
        priority=priority,
        estimated_tokens=estimate(content),
        trust=ContextTrust.UNTRUSTED_REFERENCE,
        metadata=item_metadata,
    )


def _record_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "locked",
        "evidence_mode",
        "chunk_id",
        "chunk_ordinal",
        "chunk_total",
        "start_char",
        "end_char",
        "source_sha256",
    )
    return {key: raw[key] for key in keys if key in raw}


def _unprepared_result(started: int) -> SourceCollectResult:
    return SourceCollectResult(
        adapter_key="task_document_evidence",
        kind=ContextKind.EVIDENCE,
        items=[],
        attempted=True,
        degraded=True,
        failure_code="context.source.requirement_evidence_unprepared",
        latency_ms=_now_ms() - started,
    )


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
