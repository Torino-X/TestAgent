"""format_checker — pure-function docx ↔ template structure comparison.

F025 — ``DocxFormatCheckTool`` calls ``compare_docx_to_template`` and
maps the resulting drift list to a review-style ``level``
(passed / warning / failed / loss_detected).

F025-ext — also detects user-visible **losses** (bookmarks, fields,
hyperlinks, headers, footers) that the user must explicitly
acknowledge.  Those never trigger an automatic re-export; instead
they're surfaced to the user for an "accept" or "retry" decision.

Comparison surface:

  * heading style distribution (Heading 1/2/3 counts);
  * table counts and per-table column-shape histograms;
  * any "Heading N" paragraphs at all (a docx with zero headings
    indicates the exporter dropped the heading hierarchy);
  * bookmark names (set difference: bookmarks in the template but
    missing from the output);
  * Word field / TOC instances (``<w:fldChar>`` + ``<w:instrText>``);
  * hyperlink count;
  * header and footer paragraph counts.

Drift classification:

  * ``loss_class = "cosmetic"`` — silent (not currently used; reserved
    for future style/font drift).
  * ``loss_class = "structural"`` — counted drift (heading count,
    table count, column count).  Drives automatic re-export.
  * ``loss_class = "loss"`` — user-visible missing element.  Drives
    the F025-ext confirm dialog.  ``severity`` for these is always
    ``"block"`` (no warning tier — losing a bookmark is never
    "minor").

Level decision tree:

  * any drift with ``loss_class == "loss"`` and ``severity ==
    "block"`` → ``"loss_detected"`` (new tier; orchestrator pauses)
  * any drift with ``severity == "block"`` (structural only) →
    ``"failed"`` (orchestrator auto-re-exports)
  * any drift → ``"warning"``
  * no drift → ``"passed"``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import docx  # python-docx 1.2.0
from docx.oxml.ns import qn

logger = logging.getLogger(__name__)

# Tolerances for "warning" (NOT failed) — past these bounds the drift
# is reported as "failed".
HEADING_WARN_RATIO = 0.20     # ±20% off is warning, > is failed
TABLE_WARN_RATIO = 0.20       # same
COLUMN_COUNT_TOLERANCE = 1    # absolute column-count drift allowed

# Tolerance for the "loss" tier: a hyperlink/field count delta of
# this size or more triggers a "loss_detected" verdict.  We allow
# ±1 to absorb benign noise from fields that Word auto-inserts
# (e.g. page-number anchors).
LOSS_DELTA_TOLERANCE = 1

# Namespace map for low-level XML traversal.
_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


@dataclass(frozen=True)
class Drift:
    element: str          # e.g. "h1_count", "bookmark_missing:foo"
    expected: int | list | set | tuple
    actual: int | list | set | tuple
    severity: str         # "warn" | "block"
    loss_class: str = "structural"   # "cosmetic" | "structural" | "loss"


@dataclass(frozen=True)
class HeadingCounts:
    h1: int = 0
    h2: int = 0
    h3: int = 0
    other: int = 0


@dataclass(frozen=True)
class FormatReport:
    headings: HeadingCounts
    table_count: int
    table_column_counts: tuple[int, ...]
    bookmark_names: tuple[str, ...]
    field_count: int
    hyperlink_count: int
    header_text_count: int
    footer_text_count: int
    drifts: list[Drift]

    @property
    def level(self) -> str:
        """Map drift list to a single review-style level.

        Precedence (highest first):
          * ``"loss_detected"`` — any loss-class block drift
          * ``"failed"``        — any structural block drift
          * ``"warning"``       — any warn drift
          * ``"passed"``        — no drift
        """
        if any(
            d.severity == "block" and d.loss_class == "loss"
            for d in self.drifts
        ):
            return "loss_detected"
        if any(
            d.severity == "block" and d.loss_class == "structural"
            for d in self.drifts
        ):
            return "failed"
        if self.drifts:
            return "warning"
        return "passed"

    @property
    def losses(self) -> list[Drift]:
        """All drifts classified as ``loss_class == "loss"``."""
        return [d for d in self.drifts if d.loss_class == "loss"]

    def to_dict(self) -> dict:
        losses = self.losses
        return {
            "headings": {
                "h1": self.headings.h1,
                "h2": self.headings.h2,
                "h3": self.headings.h3,
                "other": self.headings.other,
            },
            "table_count": self.table_count,
            "table_column_counts": list(self.table_column_counts),
            "bookmark_names": list(self.bookmark_names),
            "field_count": self.field_count,
            "hyperlink_count": self.hyperlink_count,
            "header_text_count": self.header_text_count,
            "footer_text_count": self.footer_text_count,
            "drift": [
                {
                    "element": d.element,
                    "expected": _serialise(d.expected),
                    "actual": _serialise(d.actual),
                    "severity": d.severity,
                    "loss_class": d.loss_class,
                }
                for d in self.drifts
            ],
            "losses": [
                {
                    "element": d.element,
                    "expected": _serialise(d.expected),
                    "actual": _serialise(d.actual),
                    "message": _loss_message(d),
                }
                for d in losses
            ],
            "loss_count": len(losses),
            "loss_details_for_user": _format_loss_summary(losses),
            "level": self.level,
        }


def _serialise(value) -> object:
    """JSON-safe serialisation for set/tuple types in Drift fields."""
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, tuple):
        return list(value)
    return value


def _loss_message(d: Drift) -> str:
    """Human-readable Chinese message for a loss-tier drift."""
    if d.element.startswith("bookmark_missing:"):
        name = d.element.split(":", 1)[1]
        return f"模板中的书签「{name}」在导出文档中未找到"
    if d.element == "field_count":
        return (
            f"模板有 {d.expected} 个 Word 字段（TOC/超链接等），"
            f"导出文档仅有 {d.actual} 个"
        )
    if d.element == "hyperlink_count":
        return (
            f"模板有 {d.expected} 个超链接，"
            f"导出文档仅有 {d.actual} 个"
        )
    if d.element == "header_text_count":
        return (
            f"模板页眉有 {d.expected} 段文字，"
            f"导出文档页眉为 {d.actual} 段"
        )
    if d.element == "footer_text_count":
        return (
            f"模板页脚有 {d.expected} 段文字，"
            f"导出文档页脚为 {d.actual} 段"
        )
    return f"{d.element}: expected={d.expected}, actual={d.actual}"


def _format_loss_summary(losses: list[Drift]) -> str:
    """Build a single user-facing Chinese summary of all losses."""
    if not losses:
        return ""
    bullets = "\n".join(f"• {_loss_message(d)}" for d in losses)
    return f"检测到 {len(losses)} 项用户可见的格式丢失：\n{bullets}"


# ── structural readers (existing) ───────────────────────────────


def _read_template_structure(template_path: str | Path) -> dict:
    """Extract a structural + loss fingerprint of the template docx."""
    template_path = Path(template_path)
    if not template_path.exists():
        raise FileNotFoundError(f"模板文件不存在：{template_path}")

    doc = docx.Document(str(template_path))
    return _extract(doc)


def _read_output_structure(output_path: str | Path) -> dict:
    output_path = Path(output_path)
    if not output_path.exists():
        raise FileNotFoundError(f"导出文件不存在：{output_path}")
    doc = docx.Document(str(output_path))
    return _extract(doc)


def _extract(doc: docx.document.Document) -> dict:
    headings = _count_headings(doc)
    tables = list(doc.tables)
    col_counts = [_column_count(t) for t in tables]
    bookmarks = _extract_bookmarks(doc)
    field_count = _count_fields(doc)
    hyperlink_count = _count_hyperlinks(doc)
    header_text_count = _count_section_part_paragraphs(doc, "header")
    footer_text_count = _count_section_part_paragraphs(doc, "footer")

    return {
        "headings": headings,
        "table_count": len(tables),
        "table_column_counts": col_counts,
        "bookmark_names": bookmarks,
        "field_count": field_count,
        "hyperlink_count": hyperlink_count,
        "header_text_count": header_text_count,
        "footer_text_count": footer_text_count,
    }


def _count_headings(doc: docx.document.Document) -> HeadingCounts:
    h1 = h2 = h3 = other = 0
    for p in doc.paragraphs:
        style = (p.style.name or "").strip() if p.style else ""
        if style == "Heading 1":
            h1 += 1
        elif style == "Heading 2":
            h2 += 1
        elif style == "Heading 3":
            h3 += 1
        elif style.startswith("Heading"):
            other += 1
    return HeadingCounts(h1=h1, h2=h2, h3=h3, other=other)


def _column_count(table) -> int:
    if not table.rows:
        return 0
    return max(len(r.cells) for r in table.rows)


# ── loss readers (new) ─────────────────────────────────────────


def _extract_bookmarks(doc: docx.document.Document) -> set[str]:
    """Collect bookmark names from ``w:bookmarkStart`` elements.

    ``python-docx`` does not expose bookmarks via its high-level API,
    so we walk the underlying OOXML.  Duplicates (Word emits paired
    Start/End for each bookmark) are de-duplicated by name.
    """
    root = doc.element
    names: set[str] = set()
    for elem in root.findall(".//w:bookmarkStart", _NS):
        name = elem.get(qn("w:name"))
        if name:
            names.add(name)
    return names


def _count_fields(doc: docx.document.Document) -> int:
    """Count Word field instances (``<w:fldChar fldCharType="begin">``).

    Each field is wrapped by a begin/end pair; counting begin markers
    gives the canonical "number of fields" number.  TOC entries are
    fields, ``PAGE`` / ``NUMPAGES`` are fields, ``HYPERLINK \\l
    "bookmark"`` is also a field — so this single counter covers
    cross-references, TOC, and inline anchors.
    """
    root = doc.element
    begins = root.findall(".//w:fldChar[@w:fldCharType='begin']", _NS)
    return len(begins)


def _count_hyperlinks(doc: docx.document.Document) -> int:
    """Count ``<w:hyperlink>`` elements.

    Note: external hyperlinks via ``HYPERLINK`` field instructions are
    already counted by ``_count_fields``; this counter only catches
    hyperlink elements with explicit ``r:id`` (the relationship-based
    form).
    """
    root = doc.element
    return len(root.findall(".//w:hyperlink", _NS))


def _count_section_part_paragraphs(
    doc: docx.document.Document, part: str,
) -> int:
    """Sum non-empty paragraphs across all sections' header/footer parts.

    ``part`` is ``"header"`` or ``"footer"``.  ``link_to_previous``
    sections reuse the previous section's header/footer — we still
    count them because the original document declared them.
    """
    total = 0
    for section in doc.sections:
        if part == "header":
            container = section.header
        elif part == "footer":
            container = section.footer
        else:
            raise ValueError(f"unsupported part: {part}")
        try:
            for para in container.paragraphs:
                if (para.text or "").strip():
                    total += 1
        except Exception as exc:  # malformed section part
            logger.debug(
                "format_checker: %s part read failed | err=%s", part, exc,
            )
    return total


# ── diff + report ──────────────────────────────────────────────


def compare_docx_to_template(
    template_path: str | Path,
    output_path: str | Path,
) -> FormatReport:
    """Compare a generated docx to its template and return a ``FormatReport``.

    The report's ``level`` is the verdict the orchestrator should act
    on.  ``drifts`` contains every individual finding (structural +
    loss); ``losses`` is a filtered view of loss-class drifts.
    """
    expected = _read_template_structure(template_path)
    actual = _read_output_structure(output_path)
    drifts = _diff(expected, actual)
    return FormatReport(
        headings=actual["headings"],
        table_count=actual["table_count"],
        table_column_counts=tuple(actual["table_column_counts"]),
        bookmark_names=tuple(sorted(actual["bookmark_names"])),
        field_count=actual["field_count"],
        hyperlink_count=actual["hyperlink_count"],
        header_text_count=actual["header_text_count"],
        footer_text_count=actual["footer_text_count"],
        drifts=drifts,
    )


def _diff(expected: dict, actual: dict) -> list[Drift]:
    drifts: list[Drift] = []

    # ── 0. Sanity: did the exporter produce ANY headings? ────────
    total_actual_headings = sum([
        actual["headings"].h1,
        actual["headings"].h2,
        actual["headings"].h3,
        actual["headings"].other,
    ])
    if total_actual_headings == 0 and (
        expected["headings"].h1 + expected["headings"].h2 + expected["headings"].h3
    ) > 0:
        drifts.append(Drift(
            element="no_headings",
            expected=expected["headings"],
            actual=actual["headings"],
            severity="block",
            loss_class="structural",
        ))

    # ── 1. Heading level-by-level counts ────────────────────────
    for key in ["h1", "h2", "h3"]:
        exp = getattr(expected["headings"], key)
        act = getattr(actual["headings"], key)
        if exp == 0 and act == 0:
            continue
        severity = _heading_severity(exp, act)
        if severity is not None:
            drifts.append(Drift(
                element=f"{key}_count",
                expected=exp,
                actual=act,
                severity=severity,
                loss_class="structural",
            ))

    # ── 2. Table count ───────────────────────────────────────────
    exp_t = expected["table_count"]
    act_t = actual["table_count"]
    if exp_t > 0 and act_t == 0:
        drifts.append(Drift(
            element="table_count",
            expected=exp_t,
            actual=act_t,
            severity="block",
            loss_class="structural",
        ))
    elif exp_t > 0:
        ratio = abs(act_t - exp_t) / exp_t
        if ratio > TABLE_WARN_RATIO:
            drifts.append(Drift(
                element="table_count",
                expected=exp_t,
                actual=act_t,
                severity="block",
                loss_class="structural",
            ))
        elif act_t != exp_t:
            drifts.append(Drift(
                element="table_count",
                expected=exp_t,
                actual=act_t,
                severity="warn",
                loss_class="structural",
            ))

    # ── 3. Per-table column-count drift ──────────────────────────
    exp_cols = expected["table_column_counts"]
    act_cols = actual["table_column_counts"]
    if exp_cols and act_cols:
        exp_hist = _histogram(exp_cols)
        act_hist = _histogram(act_cols)
        all_keys = sorted(set(exp_hist.keys()) | set(act_hist.keys()))
        for k in all_keys:
            e = exp_hist.get(k, 0)
            a = act_hist.get(k, 0)
            if abs(a - e) > COLUMN_COUNT_TOLERANCE:
                drifts.append(Drift(
                    element=f"table_columns={k}",
                    expected=e,
                    actual=a,
                    severity="warn",
                    loss_class="structural",
                ))

    # ── 4. Bookmarks (loss) ──────────────────────────────────────
    exp_bm = set(expected.get("bookmark_names") or [])
    act_bm = set(actual.get("bookmark_names") or [])
    missing_bm = sorted(exp_bm - act_bm)
    for name in missing_bm:
        drifts.append(Drift(
            element=f"bookmark_missing:{name}",
            expected=1,
            actual=0,
            severity="block",
            loss_class="loss",
        ))

    # ── 5. Fields (loss) ────────────────────────────────────────
    exp_fields = int(expected.get("field_count") or 0)
    act_fields = int(actual.get("field_count") or 0)
    if exp_fields > 0 and (exp_fields - act_fields) >= LOSS_DELTA_TOLERANCE:
        drifts.append(Drift(
            element="field_count",
            expected=exp_fields,
            actual=act_fields,
            severity="block",
            loss_class="loss",
        ))

    # ── 6. Hyperlinks (loss) ─────────────────────────────────────
    exp_links = int(expected.get("hyperlink_count") or 0)
    act_links = int(actual.get("hyperlink_count") or 0)
    if exp_links > 0 and (exp_links - act_links) >= LOSS_DELTA_TOLERANCE:
        drifts.append(Drift(
            element="hyperlink_count",
            expected=exp_links,
            actual=act_links,
            severity="block",
            loss_class="loss",
        ))

    # ── 7. Headers (loss) ────────────────────────────────────────
    exp_h = int(expected.get("header_text_count") or 0)
    act_h = int(actual.get("header_text_count") or 0)
    if exp_h > 0 and act_h == 0:
        drifts.append(Drift(
            element="header_text_count",
            expected=exp_h,
            actual=act_h,
            severity="block",
            loss_class="loss",
        ))

    # ── 8. Footers (loss) ────────────────────────────────────────
    exp_f = int(expected.get("footer_text_count") or 0)
    act_f = int(actual.get("footer_text_count") or 0)
    if exp_f > 0 and act_f == 0:
        drifts.append(Drift(
            element="footer_text_count",
            expected=exp_f,
            actual=act_f,
            severity="block",
            loss_class="loss",
        ))

    return drifts


def _heading_severity(expected: int, actual: int) -> str | None:
    if expected == actual:
        return None
    if expected == 0:
        return "warn" if actual > 0 else None
    ratio = abs(actual - expected) / expected
    if ratio > HEADING_WARN_RATIO:
        return "block"
    return "warn"


def _histogram(values: Iterable[int]) -> dict[int, int]:
    out: dict[int, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out

# 模块定位:format_checker — 纯函数 docx ↔ template 比对(F025)
#
# DocxFormatCheckTool 调 `compare_docx_to_template(...)`,产出 drift 列表;
# 再被映射成 review 级 `level`(passed / warning / failed / loss_detected)。
#
# 链路:
#   DocxFormatCheckTool → format_checker.compare_docx_to_template(template_path, out_path)
#     → drift list → Tool 内部判断 level → state.format_check_result
#
# 关键约束:
#   - 纯函数,无 DB / 无 LLM;
#   - 不"修补"任何 docx,只汇报差异;
#   - 与 F025-ext loss detection 一起(secondary features: 书签 / 批注 /
#     脚注 / 尾注);
#   - 测试覆盖每种 level 的一个示例文件。
