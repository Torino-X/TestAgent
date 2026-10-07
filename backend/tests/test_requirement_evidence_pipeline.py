from __future__ import annotations

import pytest

from app.context_engine.evidence.requirement_pipeline import (
    RequirementEvidencePreparationError,
    RequirementEvidencePreparer,
    calculate_requirement_direct_limit,
    chunk_requirement_text,
)


def _tokens(text: str) -> int:
    return len(text)


def test_requirement_direct_limit_tracks_the_configured_model_window():
    unknown_limit = calculate_requirement_direct_limit(None)
    limit_128k = calculate_requirement_direct_limit(128_000)
    limit_200k = calculate_requirement_direct_limit(200_000)

    assert unknown_limit == 6_000
    assert 30_000 < limit_128k < 40_000
    assert 70_000 < limit_200k < 80_000
    assert unknown_limit < limit_128k < limit_200k


def test_requirement_chunks_cover_the_complete_source_without_gaps():
    source = (
        "# 1 首部\nREQ-HEAD 必须支持首次退款。\n\n"
        "# 2 中部\nREQ-MIDDLE 物流争议必须转人工。\n\n"
        "# 3 尾部\nREQ-TAIL 发布前必须完成回归。"
    )

    chunks = chunk_requirement_text(
        source,
        source_ref="file_req",
        title="requirements.md",
        max_tokens=32,
        estimate=_tokens,
    )

    assert len(chunks) >= 3
    assert "".join(chunk.source_text for chunk in chunks) == source
    assert chunks[0].start_char == 0
    assert chunks[-1].end_char == len(source)
    assert [chunk.ordinal for chunk in chunks] == list(range(1, len(chunks) + 1))
    assert all(chunk.sha256 for chunk in chunks)


@pytest.mark.asyncio
async def test_large_requirement_extracts_every_chunk_and_keeps_a_coverage_manifest():
    source = "\n\n".join(f"REQ-{index:03d} requirement detail {index}" for index in range(18))
    seen: list[str] = []

    async def extract(chunk):
        seen.append(chunk.chunk_id)
        return f"[{chunk.chunk_id}] extracted {chunk.source_text.strip()}"

    bundle = await RequirementEvidencePreparer(
        direct_limit_tokens=80,
        chunk_limit_tokens=55,
        estimate=_tokens,
    ).prepare(
        {
            "document_name": "requirements.md",
            "source_ref": "file_req",
            "text_content": source,
        },
        extractor=extract,
    )

    assert bundle.mode == "chunk_extraction"
    assert seen == [chunk.chunk_id for chunk in bundle.chunks]
    assert len(bundle.records) == len(bundle.chunks)
    assert bundle.coverage_manifest["covered_characters"] == len(source)
    assert bundle.coverage_manifest["source_characters"] == len(source)
    assert bundle.coverage_manifest["coverage_complete"] is True
    assert all(record["locked"] is True for record in bundle.records)
    assert all(record["source_sha256"] for record in bundle.records)


@pytest.mark.asyncio
async def test_large_requirement_fails_closed_when_any_chunk_extraction_is_missing():
    source = "\n\n".join(f"REQ-{index:03d} requirement detail" for index in range(15))

    async def extract(chunk):
        return "" if chunk.ordinal == 2 else f"extracted {chunk.chunk_id}"

    with pytest.raises(RequirementEvidencePreparationError) as exc:
        await RequirementEvidencePreparer(
            direct_limit_tokens=60,
            chunk_limit_tokens=45,
            estimate=_tokens,
        ).prepare(
            {"document_name": "requirements.md", "text_content": source},
            extractor=extract,
        )

    assert exc.value.code == "requirement_evidence.chunk_extraction_incomplete"
    assert exc.value.chunk_ordinal == 2
