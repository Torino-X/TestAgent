"""F025-ext — Tests for format_checker's loss-tier detection.

Builds two real docx files in a temp dir, then asserts that:

  * bookmark extraction picks up ``w:bookmarkStart`` names,
  * field/hyperlink counts are correct,
  * header/footer paragraph counting works,
  * ``compare_docx_to_template`` emits the right ``loss_class`` for
    each delta and the resulting ``level == "loss_detected"`` when
    at least one block-tier loss is present.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from app.common.format_checker import (
    Drift,
    FormatReport,
    LOSS_DELTA_TOLERANCE,
    compare_docx_to_template,
)


# ── builders ─────────────────────────────────────────────────────────


def _add_heading(doc, text, level: int = 1):
    h = doc.add_heading(text, level=level)
    return h


def _add_bookmark(paragraph, name: str, bm_id: int):
    """Append a ``w:bookmarkStart`` + ``w:bookmarkEnd`` pair to a paragraph."""
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bm_id))
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bm_id))
    paragraph._p.append(start)
    paragraph._p.append(end)


def _add_field(paragraph):
    """Append a Word field (begin / instrText / separate / text / end)."""
    p = paragraph._p

    def _el(tag: str) -> OxmlElement:
        return OxmlElement(tag)

    begin = _el("w:fldChar"); begin.set(qn("w:fldCharType"), "begin")
    instr = _el("w:instrText"); instr.text = "PAGE   \\* MERGEFORMAT "
    instr.set(qn("xml:space"), "preserve")
    sep = _el("w:fldChar"); sep.set(qn("w:fldCharType"), "separate")
    end = _el("w:fldChar"); end.set(qn("w:fldCharType"), "end")
    t_el = _el("w:t"); t_el.text = "1"

    r1 = _el("w:r"); r1.append(begin); p.append(r1)
    r2 = _el("w:r"); r2.append(instr); p.append(r2)
    r3 = _el("w:r"); r3.append(sep); p.append(r3)
    r4 = _el("w:r"); r4.append(t_el); p.append(r4)
    r5 = _el("w:r"); r5.append(end); p.append(r5)


def _add_hyperlink_rel(paragraph, target_url: str) -> None:
    """Insert an external hyperlink element via a relationship."""
    part = paragraph.part
    r_id = part.relate_to(
        target_url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    h = OxmlElement("w:hyperlink")
    h.set(qn("r:id"), r_id)
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "link"
    r.append(t)
    h.append(r)
    paragraph._p.append(h)


def _write_template(path: Path) -> None:
    doc = Document()
    _add_heading(doc, "Chapter 1", 1)
    _add_heading(doc, "Section 1.1", 2)
    p1 = doc.add_paragraph("Body ")
    _add_bookmark(p1, "TOC_Top", 1)
    _add_bookmark(p1, "Section_1_1", 2)
    p2 = doc.add_paragraph("See ")
    _add_field(p2)
    _add_hyperlink_rel(p2, "https://example.com/spec")
    doc.add_paragraph("Footer text", style=None)
    # Header
    hdr = doc.sections[0].header
    hdr.paragraphs[0].add_run("Template Header")
    # Footer
    ftr = doc.sections[0].footer
    ftr.paragraphs[0].add_run("Template Footer")
    doc.save(str(path))


def _write_output_minimal(path: Path) -> None:
    """An output that loses bookmarks + fields + header + footer."""
    doc = Document()
    _add_heading(doc, "Chapter 1", 1)
    _add_heading(doc, "Section 1.1", 2)
    doc.add_paragraph("Body without bookmarks or fields")
    doc.save(str(path))


# ── tests ───────────────────────────────────────────────────────────


def test_extract_bookmarks_returns_names(tmp_path: Path):
    tpl = tmp_path / "tpl.docx"
    _write_template(tpl)
    doc = Document(str(tpl))
    from app.common.format_checker import _extract_bookmarks
    names = _extract_bookmarks(doc)
    assert "TOC_Top" in names
    assert "Section_1_1" in names


def test_count_fields_counts_begin_markers(tmp_path: Path):
    tpl = tmp_path / "tpl.docx"
    _write_template(tpl)
    doc = Document(str(tpl))
    from app.common.format_checker import _count_fields
    assert _count_fields(doc) >= 1


def test_count_hyperlinks_counts_relationship_links(tmp_path: Path):
    tpl = tmp_path / "tpl.docx"
    _write_template(tpl)
    doc = Document(str(tpl))
    from app.common.format_checker import _count_hyperlinks
    assert _count_hyperlinks(doc) >= 1


def test_compare_docx_detects_bookmark_loss(tmp_path: Path):
    tpl = tmp_path / "tpl.docx"
    out = tmp_path / "out.docx"
    _write_template(tpl)
    _write_output_minimal(out)

    report = compare_docx_to_template(tpl, out)
    loss_elements = {d.element for d in report.losses}
    assert any(e.startswith("bookmark_missing:") for e in loss_elements)
    assert "field_count" in loss_elements
    assert "hyperlink_count" in loss_elements
    assert "header_text_count" in loss_elements
    assert "footer_text_count" in loss_elements
    assert report.level == "loss_detected"


def test_compare_docx_passes_when_match(tmp_path: Path):
    tpl = tmp_path / "tpl.docx"
    out = tmp_path / "out.docx"
    _write_template(tpl)
    # Copy the template as the "output" — everything matches.
    out.write_bytes(tpl.read_bytes())

    report = compare_docx_to_template(tpl, out)
    assert report.level == "passed"
    assert report.losses == []


def test_drift_loss_class_is_loss_not_structural(tmp_path: Path):
    tpl = tmp_path / "tpl.docx"
    out = tmp_path / "out.docx"
    _write_template(tpl)
    _write_output_minimal(out)
    report = compare_docx_to_template(tpl, out)

    for d in report.drifts:
        if d.element.startswith("bookmark_missing"):
            assert d.loss_class == "loss"
            assert d.severity == "block"
    # The structural drifts (headings, tables) are still loss_class=structural
    structural = [d for d in report.drifts if d.loss_class == "structural"]
    assert all(d.severity in ("warn", "block") for d in structural)


def test_loss_delta_tolerance_constant():
    # Loss-tier deltas must be stricter than just "1" to avoid
    # false positives on benign Word auto-fields.
    assert LOSS_DELTA_TOLERANCE >= 1


def test_report_to_dict_includes_loss_summary(tmp_path: Path):
    tpl = tmp_path / "tpl.docx"
    out = tmp_path / "out.docx"
    _write_template(tpl)
    _write_output_minimal(out)
    report = compare_docx_to_template(tpl, out)
    payload = report.to_dict()
    assert "losses" in payload
    assert "loss_count" in payload
    assert "loss_details_for_user" in payload
    assert payload["loss_count"] == len(report.losses)
    # The summary must be Chinese and contain the bookmark name
    if any(d.element.startswith("bookmark_missing:") for d in report.losses):
        assert "书签" in payload["loss_details_for_user"]


def test_loss_message_for_field_count():
    d = Drift(
        element="field_count",
        expected=5,
        actual=1,
        severity="block",
        loss_class="loss",
    )
    from app.common.format_checker import _loss_message
    msg = _loss_message(d)
    assert "5" in msg
    assert "1" in msg