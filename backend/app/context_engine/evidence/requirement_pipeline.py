"""Auditable preparation of long requirement documents.

The original parsed document remains the source of truth.  This module builds
an exact, gap-free chunk manifest and, only when the raw document is too large
for the final generation evidence budget, asks a caller-supplied extractor to
produce one traceable requirement ledger per chunk.  Missing chunks fail
closed; no prefix or tail is silently discarded.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Awaitable, Callable

from app.common.token_estimator import estimate_tokens


Estimate = Callable[[str], int]
Extractor = Callable[["RequirementEvidenceChunk"], Awaitable[str]]


@dataclass(frozen=True)
class RequirementEvidenceChunk:
    chunk_id: str
    source_ref: str
    title: str
    ordinal: int
    total: int
    start_char: int
    end_char: int
    sha256: str
    source_text: str
    estimated_tokens: int


@dataclass(frozen=True)
class RequirementEvidenceBundle:
    mode: str
    records: list[dict[str, Any]]
    chunks: list[RequirementEvidenceChunk]
    coverage_manifest: dict[str, Any]
    source_tokens: int


class RequirementEvidencePreparationError(RuntimeError):
    def __init__(self, code: str, *, chunk_ordinal: int | None = None) -> None:
        self.code = code
        self.chunk_ordinal = chunk_ordinal
        super().__init__(code)


def chunk_requirement_text(
    text: str,
    *,
    source_ref: str,
    title: str,
    max_tokens: int,
    estimate: Estimate = estimate_tokens,
) -> list[RequirementEvidenceChunk]:
    """Split text into structure-aware chunks whose spans cover it exactly."""
    if not text:
        return []
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")

    spans: list[tuple[int, int]] = []
    start = 0
    length = len(text)
    while start < length:
        end = _largest_fitting_end(text, start, max_tokens, estimate)
        if end < length:
            end = _prefer_structural_boundary(text, start, end)
        if end <= start:
            end = min(length, start + 1)
        spans.append((start, end))
        start = end

    total = len(spans)
    chunks: list[RequirementEvidenceChunk] = []
    safe_ref = source_ref or "requirement"
    safe_title = title or "parsed requirement"
    for index, (span_start, span_end) in enumerate(spans, start=1):
        source_text = text[span_start:span_end]
        digest = sha256(source_text.encode("utf-8")).hexdigest()
        chunks.append(
            RequirementEvidenceChunk(
                chunk_id=f"req-{index:04d}",
                source_ref=safe_ref,
                title=safe_title,
                ordinal=index,
                total=total,
                start_char=span_start,
                end_char=span_end,
                sha256=digest,
                source_text=source_text,
                estimated_tokens=max(1, int(estimate(source_text))),
            )
        )
    return chunks


class RequirementEvidencePreparer:
    """Choose full raw evidence or complete per-chunk extraction."""

    def __init__(
        self,
        *,
        # Compatibility fallback for isolated callers without model metadata.
        # Production test-plan generation supplies a model-aware limit derived
        # from the same profile budget used by Context Engine planning.
        direct_limit_tokens: int = 80_000,
        chunk_limit_tokens: int = 6_000,
        estimate: Estimate = estimate_tokens,
    ) -> None:
        self._direct_limit_tokens = max(1, int(direct_limit_tokens))
        self._chunk_limit_tokens = max(1, int(chunk_limit_tokens))
        self._estimate = estimate

    async def prepare(
        self,
        requirement_analysis: dict[str, Any],
        *,
        extractor: Extractor | None,
        direct_limit_tokens: int | None = None,
    ) -> RequirementEvidenceBundle:
        text = str(requirement_analysis.get("text_content") or "")
        if not text:
            raise RequirementEvidencePreparationError("requirement_evidence.empty_source")

        source_ref = str(
            requirement_analysis.get("source_ref")
            or requirement_analysis.get("requirement_file_id")
            or "requirement_analysis"
        )
        title = str(
            requirement_analysis.get("document_name")
            or requirement_analysis.get("document_title")
            or "parsed requirement"
        )
        source_tokens = max(1, int(self._estimate(text)))

        effective_direct_limit = (
            self._direct_limit_tokens
            if direct_limit_tokens is None
            else max(1, int(direct_limit_tokens))
        )

        if source_tokens <= effective_direct_limit:
            digest = sha256(text.encode("utf-8")).hexdigest()
            chunk = RequirementEvidenceChunk(
                chunk_id="req-0001",
                source_ref=source_ref,
                title=title,
                ordinal=1,
                total=1,
                start_char=0,
                end_char=len(text),
                sha256=digest,
                source_text=text,
                estimated_tokens=source_tokens,
            )
            records = [_record(chunk, text, evidence_mode="full_text")]
            return RequirementEvidenceBundle(
                mode="full_text",
                records=records,
                chunks=[chunk],
                coverage_manifest=_manifest(text, [chunk], mode="full_text"),
                source_tokens=source_tokens,
            )

        if extractor is None:
            raise RequirementEvidencePreparationError(
                "requirement_evidence.extractor_unavailable"
            )

        chunks = chunk_requirement_text(
            text,
            source_ref=source_ref,
            title=title,
            max_tokens=self._chunk_limit_tokens,
            estimate=self._estimate,
        )
        records: list[dict[str, Any]] = []
        for chunk in chunks:
            extracted = str(await extractor(chunk) or "").strip()
            if not extracted:
                raise RequirementEvidencePreparationError(
                    "requirement_evidence.chunk_extraction_incomplete",
                    chunk_ordinal=chunk.ordinal,
                )
            traceable = (
                f"[source={chunk.source_ref} chunk={chunk.chunk_id} "
                f"chars={chunk.start_char}:{chunk.end_char} sha256={chunk.sha256}]\n"
                f"{extracted}"
            )
            records.append(_record(chunk, traceable, evidence_mode="chunk_extraction"))

        manifest = _manifest(text, chunks, mode="chunk_extraction")
        if not manifest["coverage_complete"] or len(records) != len(chunks):
            raise RequirementEvidencePreparationError(
                "requirement_evidence.coverage_incomplete"
            )
        return RequirementEvidenceBundle(
            mode="chunk_extraction",
            records=records,
            chunks=chunks,
            coverage_manifest=manifest,
            source_tokens=source_tokens,
        )


def calculate_requirement_direct_limit(
    model_context_window: int | None,
    *,
    chunk_floor_tokens: int = 6_000,
) -> int:
    """Return the raw-requirement allowance for final test-plan generation.

    The allowance is derived from the same model-aware budget policy and
    non-Evidence section caps as ``test_plan.generate.outline``.  This keeps
    the six-turn conversation, template/task/system layers and output reserve
    available before deciding that a requirement must use chunk extraction.
    """
    from app.context_engine.models.enums import ContextKind
    from app.context_engine.planning.budget_calculator import ContextBudgetCalculator
    from app.context_engine.profiles.registry import TEST_PLAN_GENERATE_OUTLINE_PROFILE

    profile = TEST_PLAN_GENERATE_OUTLINE_PROFILE
    budget = ContextBudgetCalculator().calculate(
        model_context_window=model_context_window,
        policy=profile.budget_policy,
    )
    non_evidence_reserve = sum(
        int(section.max_budget_tokens or 0)
        for section in profile.all_sections
        if section.kind != ContextKind.EVIDENCE
    )
    return max(
        int(chunk_floor_tokens),
        int(budget.target_input) - non_evidence_reserve,
    )


def _record(
    chunk: RequirementEvidenceChunk,
    content: str,
    *,
    evidence_mode: str,
) -> dict[str, Any]:
    return {
        "source_ref": f"{chunk.source_ref}#{chunk.chunk_id}",
        "title": f"{chunk.title} [{chunk.ordinal}/{chunk.total}]",
        "content": content,
        "evidence_mode": evidence_mode,
        "chunk_id": chunk.chunk_id,
        "chunk_ordinal": chunk.ordinal,
        "chunk_total": chunk.total,
        "start_char": chunk.start_char,
        "end_char": chunk.end_char,
        "source_sha256": chunk.sha256,
        # Selection may exceed the nominal Evidence section cap.  Keeping every
        # prepared chunk is intentional; preflight owns the final window guard.
        "locked": True,
    }


def _manifest(
    source_text: str,
    chunks: list[RequirementEvidenceChunk],
    *,
    mode: str,
) -> dict[str, Any]:
    covered = sum(chunk.end_char - chunk.start_char for chunk in chunks)
    contiguous = bool(chunks) and chunks[0].start_char == 0
    previous_end = 0
    for chunk in chunks:
        if chunk.start_char != previous_end:
            contiguous = False
        previous_end = chunk.end_char
    contiguous = contiguous and previous_end == len(source_text)
    return {
        "mode": mode,
        "source_sha256": sha256(source_text.encode("utf-8")).hexdigest(),
        "source_characters": len(source_text),
        "covered_characters": covered,
        "chunk_count": len(chunks),
        "coverage_complete": contiguous and covered == len(source_text),
        "chunks": [
            {
                "chunk_id": chunk.chunk_id,
                "ordinal": chunk.ordinal,
                "start_char": chunk.start_char,
                "end_char": chunk.end_char,
                "sha256": chunk.sha256,
            }
            for chunk in chunks
        ],
    }


def _largest_fitting_end(
    text: str,
    start: int,
    max_tokens: int,
    estimate: Estimate,
) -> int:
    if estimate(text[start:]) <= max_tokens:
        return len(text)
    low = start + 1
    high = len(text)
    best = low
    while low <= high:
        middle = (low + high) // 2
        if estimate(text[start:middle]) <= max_tokens:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _prefer_structural_boundary(text: str, start: int, end: int) -> int:
    minimum = start + max(1, int((end - start) * 0.6))
    for separator in ("\n\n", "\n", "。", ". ", "；", "; "):
        position = text.rfind(separator, minimum, end)
        if position >= minimum:
            return position + len(separator)
    return end


__all__ = [
    "RequirementEvidenceBundle",
    "RequirementEvidenceChunk",
    "RequirementEvidencePreparationError",
    "RequirementEvidencePreparer",
    "chunk_requirement_text",
]
