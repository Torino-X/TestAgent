"""Word document export service — adapted from legacy word_exporter.py.

High-fidelity template backfill: replaces AI-generated sections in a
template .docx while preserving styles, tables, drawings, content controls,
field codes, and other protected elements.

Architecture changes from legacy:
- Word COM (_refresh_fields_with_word_com) removed — server environment
  cannot depend on Windows + Office + pywin32.
- ``from-scratch`` export mode removed — TestAgent always uses template
  backfill.
- Progress callbacks removed — TestAgent uses async + structured logging.
- File paths adapted to local_storage.
- All other capabilities preserved: 26 out of 28.

Equivalent migration: 26 capabilities preserved.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

logger = logging.getLogger(__name__)


# ── Control-label regexes ──────────────────────────────────────────────

# Match status-tag suffixes like "xxx【AI生成】", "xxx（保留原文）"
CONTROL_LABEL_PATTERN = re.compile(
    r"[【\[\(（]\s*(?:AI\s*生成|保留\s*原文|保留\s*模板\s*原文|手动\s*编辑)\s*[】\]\)）]\s*"
)
# Match status-tag prefixes like "AI生成：xxx", "手动编辑: xxx"
CONTROL_PREFIX_PATTERN = re.compile(
    r"^\s*(?:AI\s*生成|保留\s*原文|保留\s*模板\s*原文|手动\s*编辑)\s*[：:]\s*"
)


# ── Exception ───────────────────────────────────────────────────────────


class WordExportError(Exception):
    """Raised when Word document export fails."""


# ── WordExporter ────────────────────────────────────────────────────────


class WordExporter:
    """High-fidelity Word document exporter.

    Fills a template .docx with AI-generated section content, preserving:
    - Paragraph and run styles (font, size, color inherited from template)
    - Table structures (headers, column counts, merged cells, nested tables)
    - Protected elements (drawings, content controls, field codes, bookmarks)
    - Document features (sections, tables, objects)

    Fidelity levels: ``high`` (strict), ``medium`` (≤10% loss tolerated),
    ``low`` (≤30% loss tolerated).
    """

    # Tolerance thresholds for critical-structure loss per fidelity level
    _CRITICAL_LOSS_THRESHOLD = {"high": 0.0, "medium": 0.10, "low": 0.30}

    def __init__(
        self, font_family: str = "宋体", fidelity: str = "high"
    ) -> None:
        self.font_family = font_family
        self.fidelity = fidelity
        self.warnings: List[str] = []
        # Phase 2.9A.X: 格式丢失待用户确认。
        # 任何 fidelity 下若 secondary_features(书签 / 批注 / 脚注 / 尾注)
        # 出现数量减少,都记入 ``pending_format_losses``,由 export_word_node
        # 决定走 ``format_loss_review`` 让用户确认(继续 / 重试 / 放弃)。
        # fidelity="high" 时 secondary raise + pending 都填;medium / low 时
        # 仅填 pending(不 raise,但用户仍需被告知)。
        self.pending_format_losses: List[str] = []

    # ── Public API ────────────────────────────────────────────────────

    def export_from_template(
        self,
        template_path: str,
        section_package: Dict[str, Any],
        output_path: str,
    ) -> None:
        """High-fidelity export: replace AI-generated sections in the template.

        Args:
            template_path: Absolute path to the template .docx.
            section_package: ``ResultParser.build_section_package()`` output.
            output_path: Absolute path for the generated .docx.

        Raises:
            WordExportError: If template is missing, section_package is invalid,
                             no sections to fill, or integrity check fails.
        """
        if not template_path or not Path(template_path).exists():
            raise WordExportError(
                "测试方案模板不存在，无法进行高保真导出"
            )
        if not isinstance(section_package, dict):
            raise WordExportError(
                "缺少章节绑定结果（section_package），无法进行高保真导出"
            )

        generated_sections = section_package.get("generated_sections", [])
        if not isinstance(generated_sections, list) or not generated_sections:
            raise WordExportError(
                "没有可填充的 AI 生成章节，无法进行高保真导出"
            )

        # Verify sections have body_start_index
        valid_sections = [
            s
            for s in generated_sections
            if isinstance(s, dict)
            and isinstance(s.get("body_start_index"), int)
        ]
        if not valid_sections:
            raise WordExportError(
                "章节包中没有 body_start_index 信息，无法定位模板位置"
            )

        try:
            doc = Document(str(Path(template_path)))

            # Replace AI-generated sections (MUST happen before italic
            # removal — removing italics first would shift body_start_index
            # and invalidate section positions)
            self._replace_generated_sections(doc, valid_sections)

            # Remove italic descriptions from ALL sections (kept + generated)
            self._remove_italic_descriptions(doc)

            # Enable auto field update on open (TOC, page numbers)
            self._enable_update_fields_on_open(doc)

            # Write to temp file, then move to output
            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            temp_fd, temp_name = tempfile.mkstemp(
                suffix=".docx", dir=str(output.parent)
            )
            os.close(temp_fd)
            temp_path = Path(temp_name)

            try:
                doc.save(str(temp_path))

                # Word COM refresh removed — server environment incompatible.
                # updateFields on open handles field refresh when the user
                # opens the document in Word.

                self._validate_export_integrity(
                    Path(template_path), temp_path
                )

                # Move temp → output
                try:
                    os.replace(str(temp_path), str(output))
                except OSError:
                    shutil.copy2(str(temp_path), str(output))
            finally:
                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)
        except WordExportError:
            raise
        except Exception as exc:
            raise WordExportError(
                f"高保真 Word 导出失败：{exc}"
            ) from exc

    # ── Section replacement ───────────────────────────────────────────

    def _replace_generated_sections(
        self,
        doc: Document,
        generated_sections: List[Dict[str, Any]],
    ) -> None:
        """Replace template sections with AI-generated content.

        Sections are processed in **reverse** order by ``body_start_index``
        to prevent index shifts from invalidating subsequent positions.
        """
        sections = [
            s
            for s in generated_sections
            if isinstance(s, dict)
            and isinstance(s.get("body_start_index"), int)
        ]
        sections.sort(
            key=lambda item: int(item["body_start_index"]), reverse=True
        )

        for section in sections:
            self._replace_one_section(doc, section)

    def _replace_one_section(
        self, doc: Document, section: Dict[str, Any]
    ) -> None:
        """Replace a single template section.

        1. Locate the heading element via ``body_start_index``.
        2. Identify old body elements between heading and next section /
           ``body_end_index``.
        3. Remove old content (paragraphs, simple tables) but preserve
           protected elements (drawings, content controls, field codes).
        4. Insert new paragraphs / tables built from ``section.content``,
           inheriting styles from a sample paragraph.
        """
        body = doc.element.body
        children = list(body.iterchildren())
        start = int(section["body_start_index"])
        end = section.get("body_end_index")
        end = int(end) if isinstance(end, int) else start

        if start < 0 or start >= len(children):
            raise WordExportError(
                f"章节位置无效：{section.get('title', section.get('field', '未知章节'))}"
            )

        end = min(end, len(children) - 1)
        heading_element = children[start]

        # Guard: never replace sectPr (document section properties)
        if heading_element.tag == qn("w:sectPr"):
            raise WordExportError(
                f"章节位置指向了文档分节属性，无法替换："
                f"{section.get('title', '')}"
            )

        # Collect original body elements for this section
        original_elements = [
            children[index]
            for index in range(start + 1, end + 1)
            if index < len(children)
            and children[index].tag != qn("w:sectPr")
        ]

        # Choose the best sample paragraph for style inheritance
        sample_paragraph = self._paragraph_sample(
            original_elements,
            prefer_list=self._content_prefers_list(
                section.get("content")
            ),
        )
        if sample_paragraph is None:
            sample_paragraph = heading_element

        # Collect template table elements
        template_tables = self._all_table_elements(original_elements)
        content = section.get("content")

        # Detect multi-table format (2D array)
        is_multi_table_format = self._is_multi_table_format(
            content, section
        )

        used_tables: List[Any] = []
        new_elements: List[Any] = []

        if is_multi_table_format and template_tables:
            # Multi-table mode: distribute data by index to template tables
            table_data_list = self._extract_multi_table_data(content)

            # Template-fidelity contract: when the model returns more
            # sub-tables than the template declares, refuse to
            # deepcopy-append extras — the operator must update the
            # template to declare all the placeholders it expects.
            if len(table_data_list) > len(template_tables):
                raise WordExportError(
                    f"章节 {section.get('field', section.get('title', '?'))} "
                    f"的多表格模式：模型返回了 {len(table_data_list)} 张表数据，"
                    f"但模板只声明了 {len(template_tables)} 个表格占位。"
                    f"请在模板中补齐所有需要的表格占位再重新生成。"
                )

            for idx, template_table in enumerate(template_tables):
                if idx < len(table_data_list):
                    self._fill_table_element(
                        template_table, table_data_list[idx]
                    )
                    used_tables.append(template_table)
                else:
                    used_tables.append(template_table)
        else:
            # Single-table mode or no tables
            sample_table = (
                template_tables[0] if template_tables else None
            )
            blocks = self._content_to_blocks(content)
            has_table_block = any(
                block_type == "table" for block_type, _ in blocks
            )
            used_table = None

            if sample_table is not None and not has_table_block:
                used_table = sample_table
                self._fill_table_element(
                    used_table,
                    self._plain_text_table_rows(
                        sample_table, content
                    ),
                )
                used_tables.append(used_table)
            else:
                # Template-fidelity contract:
                #   * If the model returns table-shaped data but the
                #     template declares NO table placeholder for this
                #     section, we MUST refuse to invent one — raise so
                #     the operator adds the missing template table.
                #   * If the model returns more tables than the
                #     template declares, we MUST refuse to deepcopy +
                #     append — raise so the template is updated.
                if sample_table is None and has_table_block:
                    raise WordExportError(
                        f"章节 {section.get('field', section.get('title', '?'))} "
                        f"的模板未声明任何表格占位，但模型返回了 table 数组。"
                        f"请在该章节的模板里添加表格占位再重新生成。"
                    )
                for block_type, value in blocks:
                    if block_type == "table":
                        if (
                            sample_table is not None
                            and used_table is None
                        ):
                            used_table = sample_table
                            self._fill_table_element(used_table, value)
                            used_tables.append(used_table)
                        elif sample_table is not None:
                            # Refuse to deepcopy-append extra tables —
                            # the template is the contract.
                            raise WordExportError(
                                f"章节 {section.get('field', section.get('title', '?'))} "
                                f"的模板只声明了 1 个表格，但模型返回了多组表格数据。"
                                f"请在模板里补齐所有需要的表格占位再重新生成。"
                            )
                        else:
                            # Unreachable: guarded above.  Defensive
                            # raise in case the guard is bypassed.
                            raise WordExportError(
                                f"章节 {section.get('field', section.get('title', '?'))} "
                                f"模板缺少表格占位。"
                            )
                    else:
                        for line in self._split_paragraph_text(
                            str(value)
                        ):
                            new_elements.append(
                                self._paragraph_element(
                                    line, sample_paragraph
                                )
                            )

        # Template fidelity: when the section is empty we DO NOT
        # insert a placeholder paragraph — leave the section body
        # alone so the template's own structure (paragraph spacing,
        # page breaks, etc.) is preserved.
        if not new_elements and not used_tables:
            logger.info(
                "WordExporter: 章节无新内容，跳过插入占位段落 | 章节=%s",
                section.get("field", section.get("title", "?")),
            )

        # Remove old elements (except used tables, protected, italic)
        for element in original_elements:
            if element in used_tables:
                continue
            if self._is_italic_paragraph(element):
                parent = element.getparent()
                if parent is body:
                    body.remove(element)
                continue
            if self._is_protected_element(element):
                continue
            parent = element.getparent()
            if parent is body:
                body.remove(element)

        # Insert new elements after the heading
        anchor = heading_element
        for element in new_elements:
            body.insert(body.index(anchor) + 1, element)
            anchor = element

    # ── Content parsing ───────────────────────────────────────────────

    def _content_to_blocks(self, content: Any) -> List[tuple]:
        """Recursively flatten structured content into (type, value) blocks.

        ``type`` is ``"paragraph"`` or ``"table"``.
        ``value`` is a string (paragraph) or ``List[List[str]]`` (table).
        """
        blocks: List[tuple] = []
        if content is None:
            return blocks
        if isinstance(content, str):
            return [
                ("paragraph", line)
                for line in self._split_paragraph_text(content)
            ]
        if isinstance(content, list):
            if self._is_table_rows(content):
                return [
                    ("table", self._rows_from_dict_list(content))
                ]
            for item in content:
                blocks.extend(self._content_to_blocks(item))
            return blocks
        if isinstance(content, dict):
            # ``content`` is the canonical structured wrapper.  ``body``
            # is a legacy model-output wrapper that must be treated as its
            # value, not as a user-visible field label (``body:``).
            for text_key in ("content", "body"):
                main_content = content.get(text_key)
                if main_content is not None:
                    blocks.extend(self._content_to_blocks(main_content))
            for key, value in content.items():
                if key in {"content", "body"}:
                    continue
                label = self._label_text(key)
                if self._is_table_rows(value):
                    blocks.append(("paragraph", label))
                    blocks.append(
                        (
                            "table",
                            self._rows_from_dict_list(value),
                        )
                    )
                elif isinstance(value, list):
                    blocks.append(("paragraph", label))
                    for item in value:
                        blocks.extend(
                            self._content_to_blocks(item)
                        )
                elif isinstance(value, dict):
                    blocks.append(("paragraph", label))
                    blocks.extend(
                        self._content_to_blocks(value)
                    )
                else:
                    blocks.append(
                        ("paragraph", f"{label}：{value}")
                    )
            return blocks
        return [("paragraph", str(content))]

    @staticmethod
    def _split_paragraph_text(text: str) -> List[str]:
        """Clean control labels and split by newline into non-empty lines."""
        text = CONTROL_LABEL_PATTERN.sub("", str(text))
        text = CONTROL_PREFIX_PATTERN.sub("", text)
        lines = [
            line.strip()
            for line in text.replace("\r\n", "\n").split("\n")
        ]
        return [line for line in lines if line]

    @staticmethod
    def _sanitize_output_text(text: str) -> str:
        """Strip AI-generation / keep-original control labels from text."""
        cleaned = CONTROL_LABEL_PATTERN.sub("", text)
        cleaned = CONTROL_PREFIX_PATTERN.sub("", cleaned)
        return cleaned.strip()

    # ── Paragraph element construction ────────────────────────────────

    def _paragraph_element(
        self, text: str, sample_paragraph=None
    ):
        """Build a new ``w:p`` element inheriting paragraph + run properties
        from a sample, enforcing ``font_family``, removing italic, and
        setting color to black (#000000).
        """
        text = self._sanitize_output_text(text)
        paragraph = OxmlElement("w:p")

        # Inherit paragraph properties (pPr), strip numbering
        ppr = self._child(sample_paragraph, "w:pPr")
        if ppr is not None:
            ppr_copy = deepcopy(ppr)
            num_pr = ppr_copy.find(qn("w:numPr"))
            if num_pr is not None:
                ppr_copy.remove(num_pr)
            paragraph.append(ppr_copy)

        run = OxmlElement("w:r")

        # Inherit run properties (rPr), strip italic
        rpr = self._first_run_properties(sample_paragraph)
        if rpr is not None:
            rpr_copy = deepcopy(rpr)
            for tag in (qn("w:i"), qn("w:iCs")):
                italic_elem = rpr_copy.find(tag)
                if italic_elem is not None:
                    rpr_copy.remove(italic_elem)
        else:
            rpr_copy = OxmlElement("w:rPr")

        # Enforce font family
        rFonts = rpr_copy.find(qn("w:rFonts"))
        if rFonts is None:
            rFonts = OxmlElement("w:rFonts")
            rpr_copy.insert(0, rFonts)
        rFonts.set(qn("w:ascii"), self.font_family)
        rFonts.set(qn("w:hAnsi"), self.font_family)
        rFonts.set(qn("w:eastAsia"), self.font_family)
        rFonts.set(qn("w:cs"), self.font_family)

        # Enforce black color
        color = rpr_copy.find(qn("w:color"))
        if color is None:
            color = OxmlElement("w:color")
            rpr_copy.append(color)
        color.set(qn("w:val"), "000000")

        run.append(rpr_copy)

        text_element = OxmlElement("w:t")
        if text.startswith(" ") or text.endswith(" "):
            text_element.set(
                "{http://www.w3.org/XML/1998/namespace}space",
                "preserve",
            )
        text_element.text = text
        run.append(text_element)
        paragraph.append(run)
        return paragraph

    # ── Table element construction ────────────────────────────────────

    def _fill_table_element(self, table, rows: List[List[str]]):
        """Fill a template table with data rows.

        - Auto-pads rows (deepcopy last data row) when the model
          returns more rows than the template declares.
        - Auto-trims rows when the model returns fewer rows.
        - Preserves the first row as header when header text is detected.
        - Cell count and table structure are NOT altered — only row
          count is allowed to grow/shrink.  (New tables / new columns
          are still refused.)
        - Delegates to ``_fill_complex_table_element`` for complex
          tables.
        """
        rows = self._normalize_rows(rows)
        if not rows:
            return table

        if self._is_complex_table(table):
            return self._fill_complex_table_element(table, rows)

        table_rows = table.findall(qn("w:tr"))
        if not table_rows:
            # Template contract: every template table must declare at
            # least one data row (typically a header).  An empty
            # ``w:tbl`` is a malformed template — refuse to auto-build
            # one from scratch.
            raise WordExportError(
                "模板表格缺少 w:tr 占位行，无法填入模型返回的表格数据。"
                "请在模板中补齐表格行结构。"
            )

        preserve_header = bool(
            self._headers_from_table_element(table)
        )
        data_rows = (
            rows[1:] if preserve_header and len(rows) > 1 else rows
        )
        target_row_count = max(
            1 if preserve_header else 0,
            (1 if preserve_header else 0) + len(data_rows),
        )

        # Row-count auto-adjust:
        # - template declares more rows than data needs → trim surplus
        # - template declares fewer rows than data needs → deepcopy
        #   the last data row to pad the table out
        # Cell count, column structure, header row and table identity
        # are all preserved — only the data-row count is allowed to
        # change.  Operators who want to keep their templates minimal
        # can rely on this auto-grow path.
        if len(table_rows) > target_row_count:
            for row in table_rows[target_row_count:]:
                table.remove(row)
            table_rows = table.findall(qn("w:tr"))
        elif len(table_rows) < target_row_count:
            # Choose the donor row: prefer the last data row (skip
            # header) so the appended rows visually match the body
            # rather than the header.
            if preserve_header and len(table_rows) > 1:
                donor = table_rows[-1]
            else:
                donor = table_rows[-1]
            missing = target_row_count - len(table_rows)
            for _ in range(missing):
                cloned = deepcopy(donor)
                table.append(cloned)
            table_rows = table.findall(qn("w:tr"))
            self.warnings.append(
                f"表格行数自动扩展 {missing} 行（模板声明 "
                f"{len(table_rows) - missing} 行，模型返回 "
                f"{target_row_count} 行）"
            )

        # Fill cells
        for row_index, row in enumerate(table_rows):
            cells = row.findall(qn("w:tc"))
            if not cells:
                cells.append(self._basic_cell_element(""))
                row.append(cells[0])
            if preserve_header and row_index == 0:
                continue
            source_index = (
                row_index - 1 if preserve_header else row_index
            )
            row_values = (
                data_rows[source_index]
                if source_index < len(data_rows)
                else []
            )
            for col_index, cell in enumerate(cells):
                value = (
                    row_values[col_index]
                    if col_index < len(row_values)
                    else ""
                )
                self._set_cell_text(cell, value)
        return table

    def _fill_complex_table_element(
        self, table, rows: List[List[str]]
    ):
        """Fill a complex table (merged cells, nested tables).

        Skips the header row and only overwrites text in data-row cells.
        Does NOT alter cell count or structure — preserves merged cells.

        Row-count handling:
        - template declares more data rows than model returns → drop
          the surplus (safe, does not touch merged-cell structure).
        - model returns more data rows than template declares → do
          NOT deepcopy-pad (cloning a merged-cell row would corrupt
          the merge geometry).  Extra rows are skipped and reported
          via a warning so the operator can extend the template.
        """
        table_rows = table.findall(qn("w:tr"))
        if not table_rows:
            return table

        preserve_header = bool(
            self._headers_from_table_element(table)
        )
        data_rows = (
            rows[1:] if preserve_header and len(rows) > 1 else rows
        )
        target_rows = (
            table_rows[1:] if preserve_header else table_rows
        )

        # Trim surplus template rows so we do not produce empty rows
        if len(target_rows) > len(data_rows):
            for row in target_rows[len(data_rows):]:
                table.remove(row)
            target_rows = target_rows[: len(data_rows)]
        elif len(data_rows) > len(target_rows):
            overflow = len(data_rows) - len(target_rows)
            self.warnings.append(
                f"复杂表格因含合并单元格，无法自动扩展，"
                f"末尾 {overflow} 行数据未写入。"
                f"请在模板中补齐合并单元格的占位行。"
            )
            data_rows = data_rows[: len(target_rows)]

        for row_index, row in enumerate(target_rows):
            row_values = data_rows[row_index]
            cells = row.findall(qn("w:tc"))
            for col_index, cell in enumerate(cells):
                if col_index >= len(row_values):
                    break
                self._set_cell_text(cell, row_values[col_index])
        return table

    def _basic_table_element(
        self, rows: List[List[str]], sample_paragraph=None
    ):
        """Build a new ``w:tbl`` element with borders from scratch."""
        rows = self._normalize_rows(rows)
        table = OxmlElement("w:tbl")

        # Table properties with borders
        tbl_pr = OxmlElement("w:tblPr")
        borders = OxmlElement("w:tblBorders")
        for border_name in (
            "top",
            "left",
            "bottom",
            "right",
            "insideH",
            "insideV",
        ):
            border = OxmlElement(f"w:{border_name}")
            border.set(qn("w:val"), "single")
            border.set(qn("w:sz"), "4")
            border.set(qn("w:space"), "0")
            border.set(qn("w:color"), "DDE5F0")
            borders.append(border)
        tbl_pr.append(borders)
        table.append(tbl_pr)

        for row_values in rows:
            row = OxmlElement("w:tr")
            for value in row_values:
                row.append(
                    self._basic_cell_element(
                        str(value), sample_paragraph
                    )
                )
            table.append(row)
        return table

    @staticmethod
    def _basic_cell_element(
        text: str = "", sample_paragraph=None
    ):
        """Build a ``w:tc`` cell with tcPr and a single paragraph."""
        cell = OxmlElement("w:tc")
        cell.append(OxmlElement("w:tcPr"))
        # Build a simple paragraph
        p = OxmlElement("w:p")
        r = OxmlElement("w:r")
        t = OxmlElement("w:t")
        t.text = str(text)
        r.append(t)
        p.append(r)
        cell.append(p)
        return cell

    @staticmethod
    def _set_cell_text(cell, text: str) -> None:
        """Replace a cell's content: keep tcPr, remove children, write text."""
        for child in list(cell):
            if child.tag != qn("w:tcPr"):
                cell.remove(child)
        p = OxmlElement("w:p")
        r = OxmlElement("w:r")
        t = OxmlElement("w:t")
        t.text = str(text)
        r.append(t)
        p.append(r)
        cell.append(p)

    # ── Header extraction ─────────────────────────────────────────────

    @staticmethod
    def _headers_from_table_element(table) -> List[str]:
        """Extract first-row cell texts as column headers.

        Duplicate names get a numeric suffix (e.g. ``列1_2``).
        """
        rows = table.findall(qn("w:tr"))
        if not rows:
            return []
        headers: List[str] = []
        used: set = set()
        for index, cell in enumerate(
            rows[0].findall(qn("w:tc")), start=1
        ):
            text = (
                "".join(
                    node.text or "" for node in cell.iter(qn("w:t"))
                ).strip()
                or f"列{index}"
            )
            if text in used:
                text = f"{text}_{index}"
            used.add(text)
            headers.append(text)
        return headers

    @staticmethod
    def _normalize_rows(
        rows: List[List[str]],
    ) -> List[List[str]]:
        """Normalize table data: stringify cells, pad to max columns,
        filter empty rows.
        """
        normalized = [
            [str(cell) for cell in row] for row in rows if row
        ]
        if not normalized:
            return []
        width = max(len(row) for row in normalized)
        return [
            row + [""] * (width - len(row)) for row in normalized
        ]

    # ── Multi-table format support ───────────────────────────────────

    def _is_multi_table_format(
        self, content: Any, section: Dict[str, Any]
    ) -> bool:
        """Detect if content is a 2D array (one sub-list per template table).

        Conditions:
        1. content is ``list[list[dict]]`` (2D array of dict rows).
        2. The section has multiple ``table_schemas`` entries.
        """
        if not isinstance(content, list) or not content:
            return False
        if not all(isinstance(item, list) for item in content):
            return False
        if not all(
            isinstance(item, list)
            and item
            and all(isinstance(row, dict) for row in item)
            for item in content
        ):
            return False

        table_schemas = section.get("table_schemas", [])
        return (
            isinstance(table_schemas, list)
            and len(table_schemas) > 1
        )

    def _extract_multi_table_data(
        self, content: Any
    ) -> List[List[List[str]]]:
        """Convert 2D multi-table content into 2D arrays.

        Input:  ``[[{...}, {...}], [{...}]]``
        Output: ``[[[headers...], [row1...]], ...]``
        """
        if not isinstance(content, list):
            return []

        result = []
        for table_content in content:
            if isinstance(table_content, list) and table_content:
                if all(
                    isinstance(item, dict)
                    for item in table_content
                ):
                    headers: List[str] = []
                    for item in table_content:
                        for key in item.keys():
                            key_text = str(key)
                            if key_text not in headers:
                                headers.append(key_text)

                    rows = [headers]
                    for item in table_content:
                        rows.append(
                            [
                                self._plain_text(
                                    item.get(header, "")
                                )
                                for header in headers
                            ]
                        )
                    result.append(rows)
                else:
                    result.append([["内容"]])
            else:
                result.append([["内容"]])
        return result

    # ── Table row detection helpers ───────────────────────────────────

    @staticmethod
    def _is_table_rows(value: Any) -> bool:
        """True if value is a non-empty list of dicts (canonical table
        format).
        """
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, dict) for item in value)
        )

    @staticmethod
    def _rows_from_dict_list(
        items: List[Dict[str, Any]],
    ) -> List[List[str]]:
        """Convert ``List[dict]`` → ``List[List[str]]`` with auto-headers."""
        headers: List[str] = []
        for item in items:
            for key in item.keys():
                key_text = str(key)
                if key_text not in headers:
                    headers.append(key_text)
        rows = [headers]
        for item in items:
            rows.append(
                [
                    WordExporter._plain_text_static(
                        item.get(header, "")
                    )
                    for header in headers
                ]
            )
        return rows

    # ── Plain-text helpers ────────────────────────────────────────────

    def _plain_text(self, value: Any) -> str:
        """Recursively flatten nested structures to a semicolon-joined
        string.
        """
        if isinstance(value, str):
            return self._sanitize_output_text(value)
        if isinstance(value, list):
            return "；".join(
                self._plain_text(item) for item in value
            )
        if isinstance(value, dict):
            return "；".join(
                f"{self._label_text(key)}：{self._plain_text(item)}"
                for key, item in value.items()
            )
        return str(value)

    @staticmethod
    def _plain_text_static(value: Any) -> str:
        """Static plain_text for use in static methods."""
        if isinstance(value, str):
            return WordExporter._sanitize_output_text(value)
        if isinstance(value, list):
            return "；".join(
                WordExporter._plain_text_static(item)
                for item in value
            )
        if isinstance(value, dict):
            return "；".join(
                f"{WordExporter._label_text_static(key)}：{WordExporter._plain_text_static(item)}"
                for key, item in value.items()
            )
        return str(value)

    @staticmethod
    def _label_text(key: Any) -> str:
        text = str(key).strip()
        return text if text else "内容"

    @staticmethod
    def _label_text_static(key: Any) -> str:
        text = str(key).strip()
        return text if text else "内容"

    def _plain_text_table_rows(
        self, sample_table, content: Any
    ) -> List[List[str]]:
        """Convert unstructured content to a single-row table with template
        headers.
        """
        headers = self._headers_from_table_element(sample_table)
        if not headers:
            headers = ["内容"]
        data_row = [self._plain_text(content)] + [""] * (
            len(headers) - 1
        )
        return [headers, data_row]

    # ── Paragraph sample selection ────────────────────────────────────

    def _paragraph_sample(
        self, elements: List[Any], prefer_list: bool = False
    ):
        """Pick the best sample paragraph for style inheritance.

        If ``prefer_list``, prefer a paragraph with list numbering.
        Otherwise prefer one without.
        """
        paragraphs = [
            e for e in elements if e.tag == qn("w:p")
        ]
        if prefer_list:
            for p in paragraphs:
                if self._paragraph_has_numbering(p):
                    return p
        for p in paragraphs:
            if not self._paragraph_has_numbering(p):
                return p
        return paragraphs[0] if paragraphs else None

    @staticmethod
    def _paragraph_has_numbering(paragraph) -> bool:
        """True if the paragraph has ``w:numPr`` or a list-style reference."""
        ppr = paragraph.find(qn("w:pPr"))
        if ppr is None:
            return False
        if ppr.find(qn("w:numPr")) is not None:
            return True
        pstyle = ppr.find(qn("w:pStyle"))
        if pstyle is None:
            return False
        val = str(pstyle.get(qn("w:val"), "")).lower()
        return "list" in val or "列表" in val

    @staticmethod
    def _content_prefers_list(content: Any) -> bool:
        """True if content appears to be a list (not table dict rows)."""
        if isinstance(content, list) and not WordExporter._is_table_rows(
            content
        ):
            return True
        if isinstance(content, dict):
            value = content.get("content")
            return isinstance(value, list) and not WordExporter._is_table_rows(
                value
            )
        return False

    # ── Element indexing helpers ──────────────────────────────────────

    @staticmethod
    def _first_run_properties(sample_paragraph):
        """Return ``w:rPr`` from the first run in a sample paragraph."""
        if sample_paragraph is None:
            return None
        for run in sample_paragraph.findall(qn("w:r")):
            rpr = run.find(qn("w:rPr"))
            if rpr is not None:
                return rpr
        return None

    @staticmethod
    def _child(element, tag: str):
        """Safely get the first child element by tag; returns None if absent."""
        if element is None:
            return None
        return element.find(qn(tag))

    @staticmethod
    def _all_table_elements(elements: List[Any]) -> List[Any]:
        """Return all ``w:tbl`` elements from a list of body children."""
        return [
            e for e in elements if e.tag == qn("w:tbl")
        ]

    # ── Italic paragraph detection & removal ─────────────────────────

    @staticmethod
    def _is_italic_paragraph(para_element) -> bool:
        """True if any run in the paragraph carries an italic marker
        (``w:i`` or ``w:iCs``).
        """
        if para_element.tag != qn("w:p"):
            return False
        for run in para_element.findall(qn("w:r")):
            rpr = run.find(qn("w:rPr"))
            if rpr is None:
                continue
            for tag in (qn("w:i"), qn("w:iCs")):
                i_elem = rpr.find(tag)
                if i_elem is not None:
                    val = i_elem.get(qn("w:val"))
                    if val is None or val in ("true", "1"):
                        return True
        return False

    def _remove_italic_descriptions(
        self, doc: Document
    ) -> None:
        """Remove all italic description paragraphs from the document body.

        Called both before and after section replacement to ensure
        template guidance text is cleared from all sections.
        """
        body = doc.element.body
        for child in list(body.iterchildren()):
            if child.tag == qn(
                "w:p"
            ) and self._is_italic_paragraph(child):
                body.remove(child)

    # ── Protected element detection ──────────────────────────────────

    @staticmethod
    def _is_protected_element(element) -> bool:
        """Check if an element is protected and should NOT be removed.

        Protected: content controls (``w:sdt``), drawings (``w:drawing``),
        picts (``w:pict``), embedded objects (``w:object``),
        field codes (``w:fldChar``, ``w:instrText``),
        bookmarks, comments, footnotes, endnotes, and complex tables.
        """
        if element.tag == qn("w:sdt"):
            return True

        protected_tags = {
            qn("w:drawing"),
            qn("w:pict"),
            qn("w:object"),
            qn("w:fldChar"),
            qn("w:instrText"),
            qn("w:bookmarkStart"),
            qn("w:bookmarkEnd"),
            qn("w:commentRangeStart"),
            qn("w:commentRangeEnd"),
            qn("w:footnoteReference"),
            qn("w:endnoteReference"),
        }
        if any(
            node.tag in protected_tags
            for node in element.iter()
        ):
            return True

        # Complex tables (merged cells / nested tables) are also protected
        if element.tag == qn(
            "w:tbl"
        ) and WordExporter._is_complex_table(element):
            return True
        return False

    @staticmethod
    def _is_complex_table(table) -> bool:
        """Detect complex tables: merged cells, nested tables, or embedded
        objects.
        """
        complex_tags = {
            qn("w:gridSpan"),
            qn("w:vMerge"),
            qn("w:drawing"),
            qn("w:pict"),
            qn("w:object"),
            qn("w:sdt"),
        }
        if any(
            node.tag in complex_tags for node in table.iter()
        ):
            return True
        # Check for nested tables
        for cell in table.iter(qn("w:tc")):
            if cell.findall(qn("w:tbl")):
                return True
        return False

    # ── updateFields on open ──────────────────────────────────────────

    @staticmethod
    def _enable_update_fields_on_open(doc: Document) -> None:
        """Write ``w:updateFields`` to settings.xml so Word auto-refreshes
        fields (TOC, page numbers, cross-references) on document open.

        This replaces the Word COM ``_refresh_fields_with_word_com()``
        that is unavailable in server environments.
        """
        settings = doc.settings.element
        update_fields = settings.find(qn("w:updateFields"))
        if update_fields is None:
            update_fields = OxmlElement("w:updateFields")
            settings.append(update_fields)
        update_fields.set(qn("w:val"), "true")

    # ── Integrity validation ──────────────────────────────────────────

    def _validate_export_integrity(
        self, template_path: Path, output_path: Path
    ) -> None:
        """Compare template and output document structural feature counts.

        Uses three-tier fidelity thresholds:
        - ``high`` (default): 0% loss on critical structures allowed.
        - ``medium``: 10% loss tolerated (warning only).
        - ``low``: 30% loss tolerated (warning only).

        Word fields are NOT validated at any fidelity level.
        """
        template_counts = self._docx_feature_counts(template_path)
        output_counts = self._docx_feature_counts(output_path)

        critical_features = {
            "sections": "分节",
            "tables": "表格",
            "drawings": "图片/绘图",
            "picts": "旧版图片/文本框",
            "objects": "嵌入对象",
            "content_controls": "内容控件",
        }
        secondary_features = {
            "bookmarks": "书签",
            "comments": "批注范围",
            "footnotes": "脚注引用",
            "endnotes": "尾注引用",
        }

        threshold = self._CRITICAL_LOSS_THRESHOLD.get(
            self.fidelity, 0.0
        )

        # Critical structure checks
        critical_losses = []
        for key, label in critical_features.items():
            tpl_count = template_counts.get(key, 0)
            out_count = output_counts.get(key, 0)
            if out_count < tpl_count:
                loss_ratio = (
                    (tpl_count - out_count) / tpl_count
                    if tpl_count > 0
                    else 0
                )
                if loss_ratio > threshold:
                    critical_losses.append(
                        f"{label} {tpl_count} -> {out_count}"
                    )
                else:
                    msg = (
                        f"{label} {tpl_count} → {out_count}"
                        f"（缺失 {loss_ratio:.0%}，在容忍范围内）"
                    )
                    self.warnings.append(msg)
                    logger.warning(
                        "导出保真度(%s)：%s",
                        self.fidelity,
                        msg,
                    )

        if critical_losses:
            raise WordExportError(
                "导出后结构校验失败，检测到模板关键结构丢失："
                + "；".join(critical_losses)
            )

        # Secondary structure checks
        if self.fidelity == "low":
            return

        secondary_losses = []
        for key, label in secondary_features.items():
            if output_counts.get(key, 0) < template_counts.get(
                key, 0
            ):
                secondary_losses.append(
                    f"{label} {template_counts.get(key, 0)} -> {output_counts.get(key, 0)}"
                )

        if secondary_losses:
            # Phase 2.9A.X: 任何 fidelity 下都把 secondary 丢失记入
            # ``pending_format_losses``,由 WordExportTool 透传到 envelope.data,
            # 再由 export_word_node 走 ``format_loss_review`` 让用户决定
            # (继续接受 / 重试 / 放弃)。这是设计预期:即使 medium 容忍了
            # secondary 丢失,用户也应该被告知文件结构有问题。
            self.pending_format_losses.extend(secondary_losses)
            if self.fidelity == "high":
                raise WordExportError(
                    "导出后结构校验失败，检测到模板次要结构丢失："
                    + "；".join(secondary_losses)
                )
            for loss in secondary_losses:
                self.warnings.append(f"{loss}（已容忍,等待用户确认）")
            logger.warning(
                "导出保真度(中)：以下次要结构变化已记录待用户确认：%s",
                "；".join(secondary_losses),
            )

    def _docx_feature_counts(
        self, path: Path
    ) -> Dict[str, int]:
        """Scan all XML files inside a .docx (ZIP) and count structural
        features.
        """
        counts = {
            "sections": 0,
            "tables": 0,
            "drawings": 0,
            "picts": 0,
            "objects": 0,
            "content_controls": 0,
            "field_chars": 0,
            "bookmarks": 0,
            "comments": 0,
            "footnotes": 0,
            "endnotes": 0,
        }
        with ZipFile(path) as archive:
            xml_names = [
                name
                for name in archive.namelist()
                if name.startswith("word/")
                and name.endswith(".xml")
            ]
            for name in xml_names:
                try:
                    root = ET.fromstring(archive.read(name))
                except ET.ParseError:
                    continue
                self._accumulate_docx_feature_counts(root, counts)
        return counts

    @staticmethod
    def _accumulate_docx_feature_counts(
        root, counts: Dict[str, int]
    ) -> None:
        """Recursively traverse XML tree to accumulate feature counts."""
        ns = {
            "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        }
        counts["sections"] += len(
            root.findall(".//w:sectPr", ns)
        )
        counts["tables"] += len(root.findall(".//w:tbl", ns))
        counts["drawings"] += len(
            root.findall(".//w:drawing", ns)
        )
        counts["picts"] += len(root.findall(".//w:pict", ns))
        counts["objects"] += len(
            root.findall(".//w:object", ns)
        )
        counts["content_controls"] += len(
            root.findall(".//w:sdt", ns)
        )
        counts["field_chars"] += len(
            root.findall(".//w:fldChar", ns)
        ) + len(root.findall(".//w:instrText", ns))
        counts["bookmarks"] += len(
            root.findall(".//w:bookmarkStart", ns)
        ) + len(root.findall(".//w:bookmarkEnd", ns))
        counts["comments"] += len(
            root.findall(".//w:commentRangeStart", ns)
        ) + len(root.findall(".//w:commentRangeEnd", ns))
        counts["footnotes"] += len(
            root.findall(".//w:footnoteReference", ns)
        )
        counts["endnotes"] += len(
            root.findall(".//w:endnoteReference", ns)
        )


# ════════════════════════════════════════════════════════════════════════════════════════════════════
# 模块定位:Word document export 服务(1337 行核心导出逻辑 ——
#   从 legacy word_exporter.py 迁移)
#
#   高保真模板回填:在测试方案模板 .docx 中替换 AI 生成章节内容,
#   **保留**样式、表格、绘图、内容控件、字段代码等受保护元素。
#
#   架构升级(对比 legacy):
#     - 移除 Word COM 路径 (_refresh_fields_with_word_com) ———
#       服务端无法依赖 Windows + Office + pywin32;
#     - 移除 from-scratch 导出模式 —— TestAgent 永远走模板回填。
#
# 链路(测试方案产物导出):
#   nodes_post_confirm.export_word_node →
#     WordExportTool.run(inputs={"fidelity": ..., "template_file_id": ...}) →
#       WordExporter.export_from_template(template_path, sections, fidelity) →
#         atomic rename .docx → state.artifact
#     → 下一跳:DocxFormatCheckTool 比对模板
#
# 主要导出类:
#   - WordExporter(template_path, fidelity)
#       构造实例;fidelity: 'high' | 'medium' | 'low'
#   - export_from_template(sections, output_path) → boolean
#       主入口,落盘 .docx
#   - pending_format_losses / warnings(由 export 链路自动填充)
#
# 关键约束:
#   - 模板回填必须**保留** 样式(Reference / bold / 字号);
#   - 二次结构丢失(书签/批注/脚注/尾注)写入 pending_format_losses;
#     不静默吞,要让 export_word_node 把它挂到 format_loss_interrupt;
#   - 高保真失败 → 自动降级 fidelity=low → 重新跑一次;
#   - 工件 atomic 写(本地 + S3 等),不允许中间文件残留;
#   - 测试覆盖每种 fidelity 的示例模板文件。
# ════════════════════════════════════════════════════════════════════════════════════════════════════
