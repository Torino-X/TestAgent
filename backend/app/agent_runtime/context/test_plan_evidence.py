"""Bounded, phase-local Evidence for test-plan repair calls.

Generation Evidence is built from parsed files.  Repair happens before an
Artifact exists, so it must instead use the generated section package and the
review findings already held in LangGraph state.  Keeping this conversion in
one module prevents the repair planner and regeneration tool from assembling
incompatible Context Engine state references.
"""

from __future__ import annotations

import json
from typing import Any


def build_test_plan_repair_state_ref(
    *,
    test_plan_content: Any,
    review_result: Any = None,
    base_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a Context Engine state reference for repair and regeneration.

    The records intentionally retain source references and are marked locked:
    a repair call must not silently drop the very generated text or review
    finding it was asked to fix.  Profile/preflight remains responsible for
    blocking an objectively oversized request.
    """
    ref: dict[str, Any] = dict(base_state or {})
    content = test_plan_content if isinstance(test_plan_content, dict) else {}
    package = content.get("section_package")
    package = package if isinstance(package, dict) else {}
    generated_sections = package.get("generated_sections")
    generated_sections = generated_sections if isinstance(generated_sections, list) else []
    # The v3 generation/review path writes directly to ``generated_sections``
    # while some older graph states retain the section package wrapper.  The
    # Context Engine repair profile requires generated evidence, so normalize
    # both persisted shapes here instead of silently dropping the direct one.
    if not generated_sections:
        direct_sections = content.get("generated_sections")
        generated_sections = direct_sections if isinstance(direct_sections, list) else []
    if not generated_sections:
        # Repair states produced by the graph before package finalisation keep
        # the same section records under ``sections``.  They are still the
        # generated artifact-under-construction and must be available to the
        # required repair Evidence section.
        graph_sections = content.get("sections")
        generated_sections = graph_sections if isinstance(graph_sections, list) else []

    generated_records: list[dict[str, Any]] = []
    for index, section in enumerate(generated_sections):
        if not isinstance(section, dict):
            continue
        rendered = _render_section(section)
        if not rendered:
            continue
        section_id = str(section.get("section_id") or section.get("id") or section.get("field") or index)
        generated_records.append(
            {
                "source_ref": f"task:generated:{section_id}",
                "title": str(section.get("title") or section.get("field") or section_id),
                "content": rendered,
                "locked": True,
            }
        )

    review_records = _review_records(review_result, content)
    ref["context_evidence"] = {
        "generated_content": generated_records,
        "review_results": review_records,
    }
    return ref


def _render_section(section: dict[str, Any]) -> str:
    content = section.get("content")
    if isinstance(content, str):
        return content.strip()
    if content is not None:
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    return ""


def _review_records(review_result: Any, test_plan_content: dict[str, Any]) -> list[dict[str, Any]]:
    source = review_result if isinstance(review_result, dict) else {}
    issues = (
        source.get("issues")
        or source.get("review_issues")
        or source.get("blocking_issues")
        or source.get("block_issues")
    )
    if not isinstance(issues, list):
        embedded_review = test_plan_content.get("review_result")
        embedded_review = embedded_review if isinstance(embedded_review, dict) else {}
        issues = (
            embedded_review.get("issues")
            or embedded_review.get("review_issues")
            or embedded_review.get("blocking_issues")
            or embedded_review.get("block_issues")
        )
    if not isinstance(issues, list):
        issues = (
            test_plan_content.get("review_issues")
            or test_plan_content.get("block_issues")
            or test_plan_content.get("schema_issues")
            or []
        )
    if not isinstance(issues, list) or not issues:
        return []

    rendered_issues: list[dict[str, Any]] = [item for item in issues if isinstance(item, dict)]
    if not rendered_issues:
        return []
    return [
        {
            "source_ref": "task:review:current",
            "title": "ResultReviewTool",
            "content": json.dumps(rendered_issues, ensure_ascii=False, sort_keys=True),
            "locked": True,
        }
    ]


__all__ = ["build_test_plan_repair_state_ref"]
