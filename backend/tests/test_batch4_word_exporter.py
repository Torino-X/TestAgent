"""Batch 4: WordExporter & WordExportTool comprehensive tests.

Covers:
- WordExporter unit tests (15 capabilities)
- WordExportTool integration tests (8 capabilities)
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

# Ensure backend on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "backend"))


# ── Test helpers ───────────────────────────────────────────────────


def _make_simple_docx() -> bytes:
    """Create a minimal docx with a heading + body paragraph + table + italic desc."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.styles["Normal"].font.size = Pt(12)

    # Section 1 heading
    h1 = doc.add_heading("1. 项目概述", level=1)
    h1_run = h1.runs[0] if h1.runs else h1.add_run("1. 项目概述")

    # Italic description
    desc = doc.add_paragraph("请在此处填写项目概述内容")
    desc.runs[0].font.italic = True if desc.runs else None

    # Body paragraph
    doc.add_paragraph("这是原有的模板正文，应该被替换。")

    # Section 2 heading with table
    doc.add_heading("2. 测试范围", level=1)

    # Italic description for section 2
    desc2 = doc.add_paragraph("请填写测试范围说明")
    if desc2.runs:
        desc2.runs[0].font.italic = True

    # Table with headers
    table = doc.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    headers = ["功能模块", "是否测试", "备注"]
    for i, h in enumerate(headers):
        cell = table.cell(0, i)
        cell.text = h
        for p in cell.paragraphs:
            for r in p.runs:
                r.bold = True
    table.cell(1, 0).text = "用户管理"
    table.cell(1, 1).text = "是"
    table.cell(1, 2).text = "核心功能"
    table.cell(2, 0).text = "课程管理"
    table.cell(2, 1).text = "是"
    table.cell(2, 2).text = ""

    # Section 3 heading
    doc.add_heading("3. 测试策略", level=1)
    doc.add_paragraph("原有策略描述文本。")

    # Save to bytes
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_docx_with_drawing() -> bytes:
    """Create a docx with a drawing (inline image)."""
    from docx import Document
    from docx.shared import Inches

    doc = Document()
    doc.add_heading("1. 测试范围", level=1)
    doc.add_paragraph("以下内容应被替换")
    # Add a drawing placeholder
    doc.add_picture(
        io.BytesIO(_make_1px_png()),
        width=Inches(1.0),
    )
    doc.add_paragraph("绘图后的文本")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_1px_png() -> bytes:
    """Minimal 1x1 pixel PNG (valid image for embedding)."""
    import struct
    import zlib

    def chunk(ctype, data):
        c = ctype + data
        return (
            struct.pack(">I", len(data))
            + c
            + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(
        b"IHDR",
        struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0),
    )
    raw = zlib.compress(b"\x00\xff\x00\x00")
    idat = chunk(b"IDAT", raw)
    iend = chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


def _make_docx_with_content_control() -> bytes:
    """Create a docx with a content control (w:sdt)."""
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    doc = Document()
    doc.add_heading("1. 审批信息", level=1)
    doc.add_paragraph("正常段落")

    # Add a content control manually
    body = doc.element.body
    sdt = OxmlElement("w:sdt")
    sdt_pr = OxmlElement("w:sdtPr")
    sdt.append(sdt_pr)
    sdt_content = OxmlElement("w:sdtContent")
    p = OxmlElement("w:p")
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "内容控件中的文本"
    r.append(t)
    p.append(r)
    sdt_content.append(p)
    sdt.append(sdt_content)
    body.append(sdt)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_docx_with_complex_table() -> bytes:
    """Create a docx with a complex table (merged cells via gridSpan)."""
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    doc = Document()
    doc.add_heading("1. 复杂表格章节", level=1)

    # Build a table with merged cells manually
    tbl = OxmlElement("w:tbl")
    tbl_pr = OxmlElement("w:tblPr")
    borders = OxmlElement("w:tblBorders")
    for bn in ("top", "left", "bottom", "right", "insideH", "insideV"):
        b = OxmlElement(f"w:{bn}")
        b.set(qn("w:val"), "single")
        b.set(qn("w:sz"), "4")
        b.set(qn("w:space"), "0")
        b.set(qn("w:color"), "auto")
        borders.append(b)
    tbl_pr.append(borders)
    tbl.append(tbl_pr)

    # Header row
    tr1 = OxmlElement("w:tr")
    tc = OxmlElement("w:tc")
    tc_pr = OxmlElement("w:tcPr")
    gs = OxmlElement("w:gridSpan")
    gs.set(qn("w:val"), "2")  # Merged 2 cols
    tc_pr.append(gs)
    tc.append(tc_pr)
    p = OxmlElement("w:p")
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "合并表头"
    r.append(t)
    p.append(r)
    tc.append(p)
    tr1.append(tc)
    tbl.append(tr1)

    # Data row
    tr2 = OxmlElement("w:tr")
    for txt in ["列1数据", "列2数据"]:
        tc2 = OxmlElement("w:tc")
        tc2.append(OxmlElement("w:tcPr"))
        p2 = OxmlElement("w:p")
        r2 = OxmlElement("w:r")
        t2 = OxmlElement("w:t")
        t2.text = txt
        r2.append(t2)
        p2.append(r2)
        tc2.append(p2)
        tr2.append(tc2)
    tbl.append(tr2)

    doc.element.body.append(tbl)
    doc.add_paragraph("表格后文本")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _section_package_for_test() -> dict:
    """Build a minimal section_package with generated_sections that map
    to the template structure from _make_simple_docx().

    Template structure (per _make_simple_docx):
      * Section 1 (项目概述): heading + italic desc + body para
      * Section 2 (测试范围): heading + italic desc + 3-row table
        (1 header row "功能模块/是否测试/备注" + 2 data row placeholders)
      * Section 3 (测试策略): heading + body para

    Per the template-fidelity contract, the model's row count for the
    table MUST NOT exceed the template's row count (1 header + N data
    placeholders).  We send exactly 2 data rows so the template's 3-row
    table accepts them without raising ``WordExportError``.
    """
    return {
        "schema_version": 1,
        "payload": {
            "overview": "智慧校园综合服务平台项目概述。本项目旨在为学校师生提供统一的数字化服务入口。",
            "scope": [
                {"模块": "用户管理", "是否测试": "是", "备注": "含登录、注册、权限控制"},
                {"模块": "课程管理", "是否测试": "是", "备注": "教师创建、学生选课"},
            ],
            "strategy": "本次测试采用黑盒功能测试为主，辅以自动化回归测试。",
        },
        "generated_sections": [
            {
                "field": "overview",
                "section_id": "sec_001",
                "title": "项目概述",
                "body_start_index": 0,
                "body_end_index": 2,
                "content": "智慧校园综合服务平台项目概述。本项目旨在为学校师生提供统一的数字化服务入口。",
            },
            {
                "field": "scope",
                "section_id": "sec_002",
                "title": "测试范围",
                "body_start_index": 3,
                "body_end_index": 5,
                "table_schemas": [{"headers": ["功能模块", "是否测试", "备注"]}],
                "content": [
                    {"模块": "用户管理", "是否测试": "是", "备注": "含登录、注册、权限控制"},
                    {"模块": "课程管理", "是否测试": "是", "备注": "教师创建、学生选课"},
                ],
            },
            {
                "field": "strategy",
                "section_id": "sec_003",
                "title": "测试策略",
                "body_start_index": 6,
                "body_end_index": 7,
                "content": "本次测试采用黑盒功能测试为主，辅以自动化回归测试。",
            },
        ],
        "keep_sections": [],
        "manual_sections": [],
    }


# ═════════════════════════════════════════════════════════════════════
# WordExporter Unit Tests
# ═════════════════════════════════════════════════════════════════════


class TestWordExporterTemplateBackfill:
    """Test high-fidelity template backfill capabilities."""

    def test_01_generate_docx_from_template(self):
        """Can generate a .docx from a template."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            exporter = WordExporter(font_family="宋体", fidelity="high")
            exporter.export_from_template(
                str(template_path),
                _section_package_for_test(),
                str(output_path),
            )

            assert output_path.exists(), "Output .docx should exist"
            assert output_path.stat().st_size > 0, "Output should not be empty"

            # Verify it's valid docx (can be opened by python-docx)
            from docx import Document
            doc = Document(str(output_path))
            assert doc is not None

    def test_02_replace_sections_via_section_package(self):
        """Sections are replaced according to body_start_index in section_package."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            exporter = WordExporter(font_family="宋体", fidelity="high")
            exporter.export_from_template(
                str(template_path),
                _section_package_for_test(),
                str(output_path),
            )

            from docx import Document
            doc = Document(str(output_path))

            # The AI content should appear in the output
            full_text = " ".join(
                p.text for p in doc.paragraphs if p.text
            )
            assert "智慧校园" in full_text, (
                f"AI-generated content should be present, got: {full_text[:200]}"
            )
            assert "黑盒功能测试" in full_text, (
                f"Strategy content should be present, got: {full_text[:200]}"
            )

    def test_03_preserve_headings(self):
        """Template headings are preserved after replacement."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            exporter = WordExporter(font_family="宋体", fidelity="high")
            exporter.export_from_template(
                str(template_path),
                _section_package_for_test(),
                str(output_path),
            )

            from docx import Document
            doc = Document(str(output_path))

            headings = [
                p.text for p in doc.paragraphs
                if p.style.name.startswith("Heading")
            ]
            assert any(
                "项目概述" in h for h in headings
            ), "Heading '项目概述' should be preserved"
            assert any(
                "测试范围" in h for h in headings
            ), "Heading '测试范围' should be preserved"
            assert any(
                "测试策略" in h for h in headings
            ), "Heading '测试策略' should be preserved"

    def test_04_remove_old_body_content(self):
        """Old template body content is removed and replaced with AI content."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            exporter = WordExporter(font_family="宋体", fidelity="high")
            exporter.export_from_template(
                str(template_path),
                _section_package_for_test(),
                str(output_path),
            )

            from docx import Document
            doc = Document(str(output_path))

            full_text = " ".join(p.text for p in doc.paragraphs if p.text)
            # Old content should NOT be present
            assert "原有的模板正文" not in full_text, (
                "Old template body should be removed"
            )
            assert "原有策略描述文本" not in full_text, (
                "Old strategy text should be removed"
            )

    def test_05_insert_paragraphs(self):
        """AI-generated paragraphs are inserted."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            exporter = WordExporter(font_family="宋体", fidelity="high")
            exporter.export_from_template(
                str(template_path),
                _section_package_for_test(),
                str(output_path),
            )

            from docx import Document
            doc = Document(str(output_path))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            # Should have more than just headings and empty lines
            assert len(paragraphs) >= 3, (
                f"Should have at least 3 non-empty paragraphs, got {len(paragraphs)}"
            )

    def test_06_insert_tables(self):
        """Tables are filled with AI-generated data."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            exporter = WordExporter(font_family="宋体", fidelity="high")
            exporter.export_from_template(
                str(template_path),
                _section_package_for_test(),
                str(output_path),
            )

            from docx import Document
            doc = Document(str(output_path))

            # Should have tables
            tables = doc.tables
            assert len(tables) >= 1, f"Should have at least 1 table, got {len(tables)}"

            # Table should contain AI-generated data
            table_text = " ".join(
                cell.text for table in tables
                for row in table.rows
                for cell in row.cells
            )
            assert "用户管理" in table_text, (
                f"Table should contain '用户管理', got: {table_text}"
            )
            assert "课程管理" in table_text, (
                f"Table should contain '课程管理', got: {table_text}"
            )

    def test_07_preserve_table_headers(self):
        """Table headers are preserved."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            exporter = WordExporter(font_family="宋体", fidelity="high")
            exporter.export_from_template(
                str(template_path),
                _section_package_for_test(),
                str(output_path),
            )

            from docx import Document
            doc = Document(str(output_path))

            tables = doc.tables
            assert len(tables) >= 1

            # First row should still be the header
            header_texts = [
                cell.text.strip() for cell in tables[0].rows[0].cells
            ]
            assert "功能模块" in header_texts, (
                f"Header '功能模块' should be preserved, got {header_texts}"
            )
            assert "是否测试" in header_texts, (
                f"Header '是否测试' should be preserved, got {header_texts}"
            )
            assert "备注" in header_texts, (
                f"Header '备注' should be preserved, got {header_texts}"
            )

    def test_08_row_pad_auto_grows_to_fit(self):
        """Row auto-grow contract: too many model rows → deepcopy-pad.

        Per the relaxed contract, the program is allowed to deepcopy
        the last data row of the template table to absorb the model's
        overflow.  Cell count, column structure, header row and table
        identity are preserved; only the data-row count grows.  The
        exporter records a warning so operators can still see that an
        auto-grow happened.
        """
        from app.common.word_exporter import WordExporter

        # Create template with a 2-row table (header + 1 data row)
        from docx import Document
        doc = Document()
        doc.add_heading("1. 测试范围", level=1)
        table = doc.add_table(rows=2, cols=2)
        table.style = "Table Grid"
        table.cell(0, 0).text = "列A"
        table.cell(0, 1).text = "列B"
        table.cell(1, 0).text = "占位A"
        table.cell(1, 1).text = "占位B"
        buf = io.BytesIO()
        doc.save(buf)
        template_bytes = buf.getvalue()

        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            exporter = WordExporter(font_family="宋体", fidelity="high")

            # 5 data rows — template only declares 1 data row, so total
            # rows needed (header + 5) = 6.  Auto-grow pads up to 6.
            section_package = {
                "generated_sections": [
                    {
                        "field": "scope",
                        "section_id": "sec_001",
                        "title": "测试范围",
                        "body_start_index": 0,
                        "body_end_index": 1,
                        "table_schemas": [{"headers": ["列A", "列B"]}],
                        "content": [
                            {"列A": "数据1", "列B": "值1"},
                            {"列A": "数据2", "列B": "值2"},
                            {"列A": "数据3", "列B": "值3"},
                            {"列A": "数据4", "列B": "值4"},
                            {"列A": "数据5", "列B": "值5"},
                        ],
                    },
                ],
            }

            exporter.export_from_template(
                str(template_path), section_package, str(output_path),
            )

            # Verify: exporter recorded a warning and the output has 6 rows.
            assert any("自动扩展" in w for w in exporter.warnings), (
                f"exporter should warn about auto-grow, got {exporter.warnings}"
            )

            from docx import Document as _D
            out_doc = _D(str(output_path))
            out_table = out_doc.tables[0]
            assert len(out_table.rows) == 6, (
                f"Output table should have header + 5 data rows = 6, "
                f"got {len(out_table.rows)}"
            )
            # Header preserved
            assert out_table.cell(0, 0).text == "列A"
            assert out_table.cell(0, 1).text == "列B"
            # First data row filled from model
            assert out_table.cell(1, 0).text == "数据1"
            # Last data row filled (was a deepcopy of donor, then overwritten
            # by the export loop with the model row "数据5")
            assert out_table.cell(5, 0).text == "数据5"

    def test_08b_row_pad_succeeds_when_template_has_room(self):
        """Counterpart to test_08: when template has enough rows, filling works."""
        from app.common.word_exporter import WordExporter

        # Template: header + 4 data rows = 5 rows total
        from docx import Document
        doc = Document()
        doc.add_heading("1. 测试范围", level=1)
        table = doc.add_table(rows=5, cols=2)
        table.style = "Table Grid"
        table.cell(0, 0).text = "列A"
        table.cell(0, 1).text = "列B"
        for i in range(1, 5):
            table.cell(i, 0).text = f"占位A{i}"
            table.cell(i, 1).text = f"占位B{i}"
        buf = io.BytesIO()
        doc.save(buf)
        template_bytes = buf.getvalue()

        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            exporter = WordExporter(font_family="宋体", fidelity="high")

            # Exactly 4 data rows — fits template's 4 placeholder rows.
            section_package = {
                "generated_sections": [
                    {
                        "field": "scope",
                        "section_id": "sec_001",
                        "title": "测试范围",
                        "body_start_index": 0,
                        "body_end_index": 1,
                        "table_schemas": [{"headers": ["列A", "列B"]}],
                        "content": [
                            {"列A": "数据1", "列B": "值1"},
                            {"列A": "数据2", "列B": "值2"},
                            {"列A": "数据3", "列B": "值3"},
                            {"列A": "数据4", "列B": "值4"},
                        ],
                    },
                ],
            }

            exporter.export_from_template(
                str(template_path), section_package, str(output_path),
            )

            result_doc = Document(str(output_path))
            tables = result_doc.tables
            assert len(tables) >= 1
            row_count = len(tables[0].rows)
            assert row_count == 5, (
                f"Table should have exactly 5 rows (header + 4 data), got {row_count}"
            )
            # Verify content was filled
            cell_a = tables[0].cell(1, 0).text.strip()
            assert cell_a == "数据1", f"Row 1 col A should be 数据1, got {cell_a!r}"

    def test_09_trim_table_rows(self):
        """Table rows are trimmed when data has fewer rows than template."""
        from app.common.word_exporter import WordExporter

        # Create template with header + 5 data rows
        from docx import Document
        doc = Document()
        doc.add_heading("1. 测试范围", level=1)
        table = doc.add_table(rows=6, cols=2)
        table.style = "Table Grid"
        table.cell(0, 0).text = "列A"
        table.cell(0, 1).text = "列B"
        for i in range(1, 6):
            table.cell(i, 0).text = f"旧数据{i}"
            table.cell(i, 1).text = f"旧值{i}"
        buf = io.BytesIO()
        doc.save(buf)
        template_bytes = buf.getvalue()

        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            exporter = WordExporter(font_family="宋体", fidelity="high")

            # Only 2 data rows — fewer than template's 5
            section_package = {
                "generated_sections": [
                    {
                        "field": "scope",
                        "section_id": "sec_001",
                        "title": "测试范围",
                        "body_start_index": 0,
                        "body_end_index": 1,
                        "table_schemas": [{"headers": ["列A", "列B"]}],
                        "content": [
                            {"列A": "新数据1", "列B": "新值1"},
                            {"列A": "新数据2", "列B": "新值2"},
                        ],
                    },
                ],
            }

            exporter.export_from_template(
                str(template_path), section_package, str(output_path),
            )

            result_doc = Document(str(output_path))
            tables = result_doc.tables
            assert len(tables) >= 1
            # Header + 2 data rows = 3
            row_count = len(tables[0].rows)
            assert row_count == 3, (
                f"Table should have exactly 3 rows (header + 2 data), got {row_count}"
            )

    def test_10_handle_complex_tables(self):
        """Complex tables (merged cells) are handled without destroying structure."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_docx_with_complex_table()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            section_package = {
                "generated_sections": [
                    {
                        "field": "complex_table",
                        "section_id": "sec_001",
                        "title": "复杂表格章节",
                        "body_start_index": 0,
                        "body_end_index": 2,
                        "content": "替换文本",
                    },
                ],
            }

            exporter = WordExporter(font_family="宋体", fidelity="high")
            exporter.export_from_template(
                str(template_path), section_package, str(output_path),
            )

            # Should not raise; verify output
            from docx import Document
            result_doc = Document(str(output_path))
            # Complex table should still exist
            tables = result_doc.tables
            assert len(tables) >= 1, "Complex table should be preserved"

    def test_11_remove_italic_descriptions(self):
        """Italic description paragraphs are removed from the document."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            exporter = WordExporter(font_family="宋体", fidelity="high")
            exporter.export_from_template(
                str(template_path),
                _section_package_for_test(),
                str(output_path),
            )

            from docx import Document
            doc = Document(str(output_path))

            # Check no italic paragraphs remain
            for p in doc.paragraphs:
                for r in p.runs:
                    assert not r.font.italic, (
                        f"Italic runs should be removed, found in: '{p.text[:50]}'"
                    )

    def test_12_protect_drawing_objects(self):
        """Drawing objects are protected from removal."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_docx_with_drawing()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            section_package = {
                "generated_sections": [
                    {
                        "field": "scope",
                        "section_id": "sec_001",
                        "title": "测试范围",
                        "body_start_index": 0,
                        "body_end_index": 3,
                        "content": "替换后的测试范围内容。",
                    },
                ],
            }

            exporter = WordExporter(font_family="宋体", fidelity="high")
            exporter.export_from_template(
                str(template_path), section_package, str(output_path),
            )

            # Verify drawing is preserved
            from docx.oxml.ns import qn
            from docx import Document
            doc = Document(str(output_path))
            body = doc.element.body
            drawings = body.findall(".//" + qn("w:drawing"))
            assert len(drawings) >= 1, (
                f"Drawing should be preserved, found {len(drawings)}"
            )

    def test_13_protect_content_controls(self):
        """Content controls are protected from removal."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_docx_with_content_control()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            section_package = {
                "generated_sections": [
                    {
                        "field": "approval",
                        "section_id": "sec_001",
                        "title": "审批信息",
                        "body_start_index": 0,
                        "body_end_index": 2,
                        "content": "替换文本",
                    },
                ],
            }

            exporter = WordExporter(font_family="宋体", fidelity="high")
            exporter.export_from_template(
                str(template_path), section_package, str(output_path),
            )

            # Verify content control is preserved
            from docx.oxml.ns import qn
            from docx import Document
            doc = Document(str(output_path))
            sdts = doc.element.body.findall(".//" + qn("w:sdt"))
            assert len(sdts) >= 1, (
                f"Content control should be preserved, found {len(sdts)}"
            )

    def test_14_write_update_fields(self):
        """updateFields is written to settings.xml."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            exporter = WordExporter(font_family="宋体", fidelity="high")
            exporter.export_from_template(
                str(template_path),
                _section_package_for_test(),
                str(output_path),
            )

            from docx import Document
            from docx.oxml.ns import qn

            doc = Document(str(output_path))
            settings = doc.settings.element
            update_fields = settings.find(qn("w:updateFields"))
            assert update_fields is not None, "updateFields should be present"
            val = update_fields.get(qn("w:val"))
            assert val == "true", (
                f"updateFields val should be 'true', got {val!r}"
            )

    def test_15_output_integrity_check_results(self):
        """Integrity validation produces results (warnings or passes)."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            exporter = WordExporter(font_family="宋体", fidelity="high")
            exporter.export_from_template(
                str(template_path),
                _section_package_for_test(),
                str(output_path),
            )

            # After export, warnings list should be populated (empty or not)
            assert isinstance(exporter.warnings, list), (
                "exporter.warnings should be a list"
            )


# ═════════════════════════════════════════════════════════════════════
# WordExporter Error Cases
# ═════════════════════════════════════════════════════════════════════


class TestWordExporterErrors:
    """Test error handling and edge cases."""

    def test_missing_template_raises_error(self):
        """Export fails with clear error when template doesn't exist."""
        from app.common.word_exporter import WordExporter, WordExportError

        with tempfile.TemporaryDirectory() as tmpdir:
            exporter = WordExporter()
            with pytest.raises(WordExportError, match="模板.*不存在"):
                exporter.export_from_template(
                    str(Path(tmpdir) / "nonexistent.docx"),
                    {"generated_sections": []},
                    str(Path(tmpdir) / "output.docx"),
                )

    def test_empty_section_package_raises_error(self):
        """Export fails when section_package has no generated_sections."""
        from app.common.word_exporter import WordExporter, WordExportError

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)

            exporter = WordExporter()
            with pytest.raises(WordExportError, match="没有可填充"):
                exporter.export_from_template(
                    str(template_path),
                    {"generated_sections": []},
                    str(Path(tmpdir) / "output.docx"),
                )

    def test_missing_section_package_raises_error(self):
        """Export fails when section_package is not a dict."""
        from app.common.word_exporter import WordExporter, WordExportError

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)

            exporter = WordExporter()
            with pytest.raises(WordExportError, match="缺少章节绑定"):
                exporter.export_from_template(
                    str(template_path),
                    None,  # type: ignore
                    str(Path(tmpdir) / "output.docx"),
                )

    def test_invalid_body_start_index_raises_error(self):
        """Export fails when body_start_index is out of range."""
        from app.common.word_exporter import WordExporter, WordExportError

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)

            section_package = {
                "generated_sections": [
                    {
                        "field": "bad",
                        "section_id": "sec_001",
                        "title": "Bad Section",
                        "body_start_index": 9999,  # way out of range
                        "body_end_index": 10000,
                        "content": "test",
                    },
                ],
            }

            exporter = WordExporter(fidelity="low")  # lower fidelity to skip integrity check
            with pytest.raises(WordExportError, match="章节位置无效"):
                exporter.export_from_template(
                    str(template_path), section_package,
                    str(Path(tmpdir) / "output.docx"),
                )


# ═════════════════════════════════════════════════════════════════════
# WordExporter Integrity Validation Tests
# ═════════════════════════════════════════════════════════════════════


class TestWordExporterIntegrity:
    """Test export integrity validation."""

    def test_high_fidelity_passes_on_identical_structure(self):
        """High fidelity should pass when structure is preserved."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)
            output_path = Path(tmpdir) / "output.docx"

            exporter = WordExporter(fidelity="high")
            exporter.export_from_template(
                str(template_path),
                _section_package_for_test(),
                str(output_path),
            )
            # Should not raise — integrity check passes or gives warnings
            assert output_path.exists()

    def test_feature_counts_are_collected(self):
        """_docx_feature_counts returns a complete dict."""
        from app.common.word_exporter import WordExporter

        template_bytes = _make_simple_docx()
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "template.docx"
            template_path.write_bytes(template_bytes)

            exporter = WordExporter()
            counts = exporter._docx_feature_counts(template_path)

            assert isinstance(counts, dict)
            assert "sections" in counts
            assert "tables" in counts
            assert "drawings" in counts
            assert "content_controls" in counts
            assert counts["tables"] >= 1, f"Template should have ≥1 table, got {counts}"


# ═════════════════════════════════════════════════════════════════════
# WordExportTool Integration Tests
# ═════════════════════════════════════════════════════════════════════



def _cleanup_artifact(artifact_id: str) -> None:
    """Delete artifact DB row + physical file after test."""
    from app.db.session import sync_engine
    from sqlalchemy import text
    with sync_engine.begin() as conn:
        row = conn.execute(text(
            "SELECT storage_path FROM artifacts WHERE public_id = :pid LIMIT 1"
        ), {"pid": artifact_id}).fetchone()
        conn.execute(text("DELETE FROM artifacts WHERE public_id = :pid"), {"pid": artifact_id})
    if row and row[0]:
        from app.storage.local_storage import local_storage
        p = local_storage._base / row[0]
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


class TestWordExportToolIntegration:
    """Test the Tool's integration with AgentContext and file resolution."""

    def test_01_read_section_package_from_context(self):
        """Tool reads section_package from context.test_plan_content."""
        import asyncio
        from app.agent.context import AgentContext
        from app.tools.word_export_tool import WordExportTool

        async def run_test():
            ctx = AgentContext(
                task_id="task_int_01",
                conversation_id="conv_int_01",
                user_id="user_int_01",
            )
            ctx.test_plan_content = {
                "generated_sections": 3,
                "kept_sections": 0,
                "section_package": _section_package_for_test(),
            }

            # Template file must exist for the tool to proceed
            # Without template, it should fail with template_not_found
            tool = WordExportTool()
            result = await tool.run({}, ctx)

            # Should fail because no template file can be found
            # (template_file_id is not set)
            assert not result["success"]
            assert result["error"]["code"] == "EXPORT_TEMPLATE_NOT_FOUND"

        asyncio.run(run_test())

    def test_02_locate_template_file(self):
        """Tool locates template via template_file_id in context.

        Phase 2.9A.13: 模板定位必须经过 Resolver,Resolver 走 DB 查
        ``uploaded_files`` 而不是磁盘扫描。本测试使用真实 DB 行
        (id=112 / public_id=file_003ccd34) 验证 WordExportTool 解析成功。
        """
        import asyncio
        from app.agent.context import AgentContext
        from app.tools.word_export_tool import WordExportTool

        async def run_test():
            # Use the real template row in dev DB.  The real template
            # file at uploads/1/conv_64ff9e63/ is the canonical fixture
            # the user used to reproduce EXPORT_TEMPLATE_NOT_FOUND.
            real_public_id = "file_003ccd34"

            ctx = AgentContext(
                task_id="task_int_02",
                conversation_id="conv_int_02",
                user_id="1",
            )
            ctx.user_internal_id = 1
            ctx.conversation_internal_id = 70
            ctx.task_internal_id = 99
            ctx.test_plan_content = {
                "generated_sections": 3,
                "section_package": _section_package_for_test(),
            }
            ctx.template_file_id = real_public_id

            tool = WordExportTool()
            result = await tool.run({"template_file_id": real_public_id}, ctx)

            assert result["success"], (
                f"Export should succeed, got error: {result.get('error')}"
            )
            assert "artifact_id" in result["data"], (
                f"Should return artifact_id, got: {result['data']}"
            )
            # Cleanup
            artifact_id = result["data"].get("artifact_id")
            if artifact_id:
                _cleanup_artifact(artifact_id)

        asyncio.run(run_test())

    def test_03_generate_docx_file(self):
        """Tool generates a real .docx file on disk."""
        import asyncio
        from app.agent.context import AgentContext
        from app.tools.word_export_tool import WordExportTool

        async def run_test():
            real_public_id = "file_003ccd34"
            ctx = AgentContext(task_id="t3", conversation_id="c3", user_id="1")
            ctx.user_internal_id = 1
            ctx.conversation_internal_id = 70
            ctx.task_internal_id = 103
            ctx.test_plan_content = {"generated_sections": 3, "section_package": _section_package_for_test()}
            result = await WordExportTool().run({"template_file_id": real_public_id}, ctx)
            assert result["success"], f"Export failed: {result.get('error')}"
            assert result["data"]["file_size"] > 0
            aid = result["data"].get("artifact_id")
            if aid: _cleanup_artifact(aid)

        asyncio.run(run_test())

    def test_04_return_artifact_id(self):
        """Tool returns artifact_id in output."""
        import asyncio
        from app.agent.context import AgentContext
        from app.tools.word_export_tool import WordExportTool

        async def run_test():
            real_public_id = "file_003ccd34"
            ctx = AgentContext(task_id="t4", conversation_id="c4", user_id="1")
            ctx.user_internal_id = 1
            ctx.conversation_internal_id = 70
            ctx.task_internal_id = 104
            ctx.test_plan_content = {"generated_sections": 3, "section_package": _section_package_for_test()}
            result = await WordExportTool().run({"template_file_id": real_public_id}, ctx)
            assert result["success"]
            aid = result["data"].get("artifact_id")
            assert aid is not None and aid.startswith("art_")
            if aid: _cleanup_artifact(aid)

        asyncio.run(run_test())

    def test_05_storage_path_in_output_for_graph_state(self):
        """Phase 2.9A.16: Tool output now includes storage_path for Graph State propagation.

        Previously storage_path was only in context.artifact, but proxy
        side-effect doesn't propagate to export nodes. Now it's in data.
        """
        import asyncio
        from app.agent.context import AgentContext
        from app.tools.word_export_tool import WordExportTool

        async def run_test():
            real_public_id = "file_003ccd34"
            ctx = AgentContext(task_id="t5", conversation_id="c5", user_id="1")
            ctx.user_internal_id = 1
            ctx.conversation_internal_id = 70
            ctx.task_internal_id = 105
            ctx.test_plan_content = {"generated_sections": 3, "section_package": _section_package_for_test()}
            result = await WordExportTool().run({"template_file_id": real_public_id}, ctx)
            assert result["success"]
            # Phase 2.9A.16: storage_path 必须在 data 中,供 Graph State 传播
            assert result["data"].get("storage_path"), "storage_path must be in data for Graph State"
            assert "artifacts" in result["data"]["storage_path"]
            aid = result["data"].get("artifact_id")
            if aid: _cleanup_artifact(aid)

        asyncio.run(run_test())

    def test_06_download_url_correct_format(self):
        """download_url follows the correct API pattern."""
        import asyncio
        from app.agent.context import AgentContext
        from app.tools.word_export_tool import WordExportTool

        async def run_test():
            real_public_id = "file_003ccd34"
            ctx = AgentContext(task_id="t6", conversation_id="c6", user_id="1")
            ctx.user_internal_id = 1
            ctx.conversation_internal_id = 70
            ctx.task_internal_id = 106
            ctx.test_plan_content = {"generated_sections": 3, "section_package": _section_package_for_test()}
            result = await WordExportTool().run({"template_file_id": real_public_id}, ctx)
            assert result["success"]
            aid = result["data"]["artifact_id"]
            assert f"/api/v1/artifacts/{aid}/download" == result["data"]["download_url"]
            if aid: _cleanup_artifact(aid)

        asyncio.run(run_test())

    def test_07_fallback_section_package_direct(self):
        """section_package can come directly in test_plan_content."""
        import asyncio
        from app.agent.context import AgentContext
        from app.tools.word_export_tool import WordExportTool

        async def run_test():
            real_public_id = "file_003ccd34"
            ctx = AgentContext(task_id="t7", conversation_id="c7", user_id="1")
            ctx.user_internal_id = 1
            ctx.conversation_internal_id = 70
            ctx.task_internal_id = 107
            # Directly embed section_package (fallback path in WordExportTool)
            ctx.test_plan_content = _section_package_for_test()
            result = await WordExportTool().run({"template_file_id": real_public_id}, ctx)
            assert result["success"], f"Export failed: {result.get('error')}"
            aid = result["data"].get("artifact_id")
            if aid: _cleanup_artifact(aid)

        asyncio.run(run_test())

    def test_08_integrity_in_output(self):
        """Result includes integrity check."""
        import asyncio
        from app.agent.context import AgentContext
        from app.tools.word_export_tool import WordExportTool

        async def run_test():
            real_public_id = "file_003ccd34"
            ctx = AgentContext(task_id="t8", conversation_id="c8", user_id="1")
            ctx.user_internal_id = 1
            ctx.conversation_internal_id = 70
            ctx.task_internal_id = 108
            ctx.test_plan_content = {"generated_sections": 3, "section_package": _section_package_for_test()}
            result = await WordExportTool().run({"template_file_id": real_public_id}, ctx)
            assert result["success"]
            assert "integrity" in result["data"]
            assert isinstance(result["data"]["integrity"], dict)
            aid = result["data"].get("artifact_id")
            if aid: _cleanup_artifact(aid)

        asyncio.run(run_test())



# ═════════════════════════════════════════════════════════════════════
# Run
# ═════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
